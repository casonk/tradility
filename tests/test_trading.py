"""Order placement: the policy fails closed, orders need a preview and a typed confirmation."""

import dataclasses
import json
import os
from datetime import datetime

import pytest

from tradility import trading as t
from tradility._compat import UTC

POLICY = t.Policy(
    enabled=True,
    per_order_usd=1000,
    daily_usd=1500,
    price_band_pct=5,
    brokers=("schwab", "binance"),
    tickers=frozenset({"AAPL", "BTC-USD"}),
    schwab_accounts=("1234", "77"),
)


class Fake:
    """A scripted broker API recording every request; nothing leaves the process."""

    def __init__(self):
        self.calls = []
        self.fail_place = None

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body))
        self.content_types = getattr(self, "content_types", []) + [
            (method, headers.get("Content-Type"))
        ]
        if url.endswith("accountNumbers"):
            return (
                200,
                {},
                [
                    {"accountNumber": "99991234", "hashValue": "HASH"},
                    {"accountNumber": "5555", "hashValue": "OTHER"},
                ],
            )
        if "/marketdata/" in url:
            symbol = url.split("symbols=")[1].split("&")[0]
            return 200, {}, {symbol: {"quote": {"lastPrice": 100.0}}}
        if "exchangeInfo" in url:
            return (
                200,
                {},
                {
                    "symbols": [
                        {
                            "symbol": "BTCUSD",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                                {
                                    "filterType": "LOT_SIZE",
                                    "minQty": "0.00001",
                                    "stepSize": "0.00001",
                                },
                                {"filterType": "NOTIONAL", "minNotional": "10"},
                            ],
                        }
                    ]
                },
            )
        if "ticker/price" in url:
            return 200, {}, {"price": "50000"}
        if url.endswith("previewOrder"):
            return 200, {}, {"orderValidationResult": {"warns": [{"message": "extended hours"}]}}
        if url.endswith("/order/test"):
            return 200, {}, {}
        if self.fail_place:
            raise self.fail_place
        if url.endswith("/accounts/HASH/orders") and method == "POST":
            return 201, {"Location": f"{t.SCHWAB_BASE}/trader/v1/accounts/HASH/orders/777"}, {}
        if url.endswith("/api/v3/order") and method == "POST":
            return 200, {}, {"orderId": 4242}
        if method == "DELETE":
            return 200, {}, {}
        return 404, {}, {"msg": "unexpected " + url}


@pytest.fixture
def rig(tmp_path):
    send = Fake()
    brokers = {
        "schwab": lambda: t.SchwabTrader("tok", send),
        "binance": lambda: t.BinanceTrader("KEY", "SECRET", send, clock=lambda: 1.0),
    }
    audit = t.Audit(tmp_path / "state" / "audit.jsonl")
    clock = {"now": 1000.0}
    trader = t.Trader(POLICY, brokers, audit, now=lambda: clock["now"])
    return trader, send, audit, clock


def order(**kw):
    return {
        "ticker": "AAPL",
        "side": "buy",
        "kind": "limit",
        "quantity": 5,
        "price": 100,
        "account": "1234",
        **kw,
    }


def placements(send):
    return [c for c in send.calls if c[0] == "POST" and c[1].endswith(("/orders", "/api/v3/order"))]


def test_a_preview_then_the_typed_phrase_places_exactly_the_previewed_order(rig):
    trader, send, audit, _ = rig
    pv = trader.preview(order())
    assert (
        pv["phrase"] == "BUY AAPL"
        and pv["warnings"] == ["extended hours"]
        and pv["notional"] == 500
    )
    assert not placements(send)  # a preview never places
    done = trader.confirm(pv["preview_id"], "BUY AAPL")
    assert done == {
        "placed": True,
        "broker": "schwab",
        "broker_order_id": "777",
        "order": pv["order"],
    }
    sent = json.loads(placements(send)[0][2])
    assert sent["orderLegCollection"][0]["instruction"] == "BUY" and sent["price"] == "100.00"
    events = [json.loads(line)["event"] for line in audit.path.read_text().splitlines()]
    assert (
        events == ["preview", "sending", "placed"] and (audit.path.stat().st_mode & 0o777) == 0o600
    )


def test_the_wrong_phrase_or_a_second_use_places_nothing(rig):
    trader, send, *_ = rig
    pv = trader.preview(order())
    with pytest.raises(t.TradingError, match='type exactly "BUY AAPL"'):
        trader.confirm(pv["preview_id"], "buy aapl please")
    with pytest.raises(t.TradingError, match="expired or was already used"):
        trader.confirm(pv["preview_id"], "BUY AAPL")  # the failed attempt used the preview up
    pv = trader.preview(order())
    trader.confirm(pv["preview_id"], "BUY AAPL")
    with pytest.raises(t.TradingError):
        trader.confirm(pv["preview_id"], "BUY AAPL")
    assert len(placements(send)) == 1


def test_a_preview_expires(rig):
    trader, send, _, clock = rig
    pv = trader.preview(order())
    clock["now"] += t.PREVIEW_TTL_SECONDS + 1
    with pytest.raises(t.TradingError, match="expired"):
        trader.confirm(pv["preview_id"], "BUY AAPL")
    assert not placements(send)


def test_everything_is_off_unless_the_policy_enables_it(tmp_path):
    assert t.load_policy(tmp_path / "missing.toml") == t.Policy()
    send = Fake()
    trader = t.Trader(
        t.Policy(),
        {"schwab": lambda: t.SchwabTrader("t", "1234", send)},
        t.Audit(tmp_path / "a.jsonl"),
    )
    with pytest.raises(t.TradingError, match="turned off"):
        trader.preview(order())
    assert not send.calls


@pytest.mark.parametrize(
    "change, message",
    [
        ({"ticker": "MSFT"}, "allowlist"),
        ({"quantity": 11}, "per-order limit"),
        ({"price": 120}, "away from the last price"),
        ({"price": 80}, "away from the last price"),
        ({"quantity": 0}, "positive"),
        ({"side": "short"}, "buy or sell"),
        ({"kind": "stop"}, "market or limit"),
        ({"kind": "limit", "price": None}, "needs a price"),
    ],
)
def test_orders_outside_the_policy_or_malformed_are_refused_before_any_broker_preview(
    rig, change, message
):
    trader, send, *_ = rig
    with pytest.raises(t.TradingError, match=message):
        trader.preview(order(**change))
    assert not [c for c in send.calls if c[1].endswith(("previewOrder", "/order/test"))]


def test_the_daily_cap_counts_sent_and_unknown_orders_from_the_audit_log(rig):
    trader, send, audit, _ = rig
    for _ in range(3):  # 3 x $500 = the $1500 cap
        pv = trader.preview(order())
        trader.confirm(pv["preview_id"], "BUY AAPL")
    assert audit.spent_today() == 1500
    with pytest.raises(t.TradingError, match="daily limit"):
        trader.preview(order())
    audit.write("unknown", notional=1, order={})
    assert audit.spent_today(datetime.now(UTC)) == 1501
    assert audit.spent_today(datetime(2001, 1, 1, tzinfo=UTC)) == 0  # another day starts clean


def test_a_timeout_is_recorded_as_unknown_counted_and_never_retried(rig):
    trader, send, audit, _ = rig
    pv = trader.preview(order())
    send.fail_place = TimeoutError("slow")
    with pytest.raises(t.TradingError, match="outcome is unknown"):
        trader.confirm(pv["preview_id"], "BUY AAPL")
    assert audit.spent_today() == 500
    assert len(placements(send)) == 1  # one attempt, never a second try


def test_a_broker_refusal_is_audited_and_not_counted(rig):
    trader, send, audit, _ = rig
    pv = trader.preview(order())
    send.fail_place = None
    send_original = send.__call__

    def reject(method, url, headers, body):
        if url.endswith("/orders") and method == "POST":
            return 400, {}, {"message": "insufficient funds"}
        return send_original(method, url, headers, body)

    trader.brokers["schwab"] = lambda: t.SchwabTrader("tok", reject)
    with pytest.raises(t.TradingError, match="insufficient funds"):
        trader.confirm(pv["preview_id"], "BUY AAPL")
    assert audit.spent_today() == 0
    assert '"failed"' in audit.path.read_text()


def test_crypto_goes_to_binance_signed_with_a_client_id_and_the_test_endpoint_for_previews(rig):
    trader, send, audit, _ = rig
    pv = trader.preview(order(ticker="BTC-USD", quantity=0.01, price=50000))
    assert pv["broker"] == "binance" and pv["phrase"] == "BUY BTC-USD"
    assert any(c[1].endswith("/api/v3/order/test") for c in send.calls)
    done = trader.confirm(pv["preview_id"], "BUY BTC-USD")
    assert done["broker_order_id"] == "4242"
    body = placements(send)[0][2].decode()
    assert "symbol=BTCUSD" in body and "side=BUY" in body and "type=LIMIT" in body
    assert "newClientOrderId=tradility-" in body and "signature=" in body
    assert (
        "SECRET" not in body
        and "SECRET" not in audit.path.read_text()
        and "KEY" not in audit.path.read_text()
    )


def test_the_schwab_account_must_match_exactly_one_account(rig):
    trader, send, *_ = rig
    with pytest.raises(t.TradingError, match="exactly one"):
        trader.preview(order(account="77"))  # allowed by the policy but matches no real account
    with pytest.raises(t.TradingError, match="accounts the policy allows"):
        trader.preview(order(account="9999"))
    with pytest.raises(t.TradingError, match="choose"):
        trader.preview(order(account=""))


def test_cancelling_needs_its_own_phrase(rig):
    trader, send, audit, _ = rig
    with pytest.raises(t.TradingError, match="CANCEL 777"):
        trader.cancel("schwab", "777", "yes")
    with pytest.raises(t.TradingError, match="accounts the policy allows"):
        trader.cancel("schwab", "777", "CANCEL 777")
    assert trader.cancel("schwab", "777", "CANCEL 777", account="1234")["cancelled"] is True
    assert any(c[0] == "DELETE" and c[1].endswith("/orders/777") for c in send.calls)
    with pytest.raises(t.TradingError):
        trader.cancel("schwab", "../x", "CANCEL ../x", account="1234")


def test_the_policy_file_must_be_owner_only_and_valid(tmp_path):
    path = tmp_path / "trading.toml"
    path.write_text(
        'enabled = true\nper_order_usd = 100\ndaily_usd = 200\nprice_band_pct = 3\ntickers = ["aapl"]\n[brokers]\nschwab = true\n[schwab]\naccounts = ["1234"]\n'
    )
    os.chmod(path, 0o644)
    with pytest.raises(t.TradingError, match="owner-only"):
        t.load_policy(path)
    os.chmod(path, 0o600)
    policy = t.load_policy(path)
    assert policy.enabled and policy.tickers == {"AAPL"} and policy.brokers == ("schwab",)
    path.write_text('enabled = "yes"\n')
    assert not t.load_policy(path).enabled  # only a real true enables trading
    path.write_text("not = [toml")
    with pytest.raises(t.TradingError, match="unreadable"):
        t.load_policy(path)


def test_schwab_gets_carry_no_content_type_and_posts_do(rig):
    trader, send, *_ = rig
    pv = trader.preview(order())
    trader.confirm(pv["preview_id"], "BUY AAPL")
    sent = send.content_types
    assert ("GET", None) in sent and all(
        ct is None for m, ct in sent if m == "GET" and ct is not None
    ) is True
    assert ("POST", "application/json") in sent


def test_a_limit_order_rests_until_cancelled_and_a_market_order_is_for_the_day(rig):
    trader, send, *_ = rig
    for kind, price, duration in (("limit", 100, "GOOD_TILL_CANCEL"), ("market", None, "DAY")):
        pv = trader.preview(order(kind=kind, price=price))
        trader.confirm(pv["preview_id"], "BUY AAPL")
        assert json.loads(placements(send)[-1][2])["duration"] == duration


def test_the_default_prices_sit_two_and_a_half_percent_either_side_of_the_mark(rig):
    trader, *_ = rig
    got = trader.quote("aapl")
    assert got["mark"] == 100.0 and got["buy_price"] == 97.5 and got["sell_price"] == 102.5
    with pytest.raises(t.TradingError, match="allowlist"):
        trader.quote("MSFT")


def test_a_star_in_the_allowlist_allows_any_ticker_but_keeps_every_other_limit(tmp_path):
    send = Fake()
    open_policy = t.Policy(**{**POLICY.__dict__, "tickers": frozenset({"*"})})
    trader = t.Trader(
        open_policy, {"schwab": lambda: t.SchwabTrader("tok", send)}, t.Audit(tmp_path / "a.jsonl")
    )
    assert trader.preview(order(ticker="MSFT", quantity=1))["phrase"] == "BUY MSFT"
    with pytest.raises(t.TradingError, match="per-order limit"):
        trader.preview(order(ticker="MSFT", quantity=50))
    assert open_policy.allows("ANYTHING") and not POLICY.allows("ANYTHING")


def test_the_quote_uses_the_mark_then_the_last_trade(rig):
    _, send, *_ = rig
    sb = t.SchwabTrader(
        "tok", lambda m, u, h, b: (200, {}, {"AAPL": {"quote": {"mark": 51.0, "lastPrice": 50.0}}})
    )
    assert sb.last_price("AAPL") == 51.0
    sb = t.SchwabTrader(
        "tok", lambda m, u, h, b: (200, {}, {"AAPL": {"quote": {"lastPrice": 50.0}}})
    )
    assert sb.last_price("AAPL") == 50.0


def test_binance_quotes_defaults_on_the_tick_and_refuses_orders_the_exchange_would_reject(rig):
    trader, send, *_ = rig
    quote = trader.quote("BTC-USD")
    assert (
        quote["mark"] == 50000.0
        and quote["buy_price"] == 48750.0
        and quote["sell_price"] == 51250.0
    )
    for change, message in (
        ({"quantity": 0.000001}, "multiple of 0.00001"),
        ({"quantity": 0.012345678}, "multiple of 0.00001"),
        ({"price": 50000.001}, "multiple of 0.01"),
        ({"quantity": 0.0001, "price": 50000}, "below Binance.US's \\$10 minimum"),
    ):
        with pytest.raises(t.TradingError, match=message):
            trader.preview(
                order(ticker="BTC-USD", account="", **{"quantity": 0.01, "price": 50000, **change})
            )
    assert not placements(send)
    ok = trader.preview(order(ticker="BTC-USD", account="", quantity=0.01, price=50000))
    assert ok["broker"] == "binance"


def body_of(send):
    return json.loads(placements(send)[-1][2])


def test_extended_hours_and_the_duration_are_chosen_per_order_and_sent_to_schwab(rig):
    trader, send, audit, _ = rig
    for extra, session, duration in (
        ({"session": "extended", "tif": "gtc"}, "SEAMLESS", "GOOD_TILL_CANCEL"),
        ({"session": "extended", "tif": "day"}, "SEAMLESS", "DAY"),
        ({"session": "normal", "tif": "gtc"}, "NORMAL", "GOOD_TILL_CANCEL"),
        ({"session": "normal", "tif": "day"}, "NORMAL", "DAY"),
    ):
        pv = trader.preview(order(quantity=1, **extra))  # $100 each, well inside the daily cap
        assert pv["order"]["session"] == extra["session"] and pv["order"]["tif"] == extra["tif"]
        trader.confirm(pv["preview_id"], "BUY AAPL")
        sent = body_of(send)
        assert (sent["session"], sent["duration"]) == (session, duration)
    assert '"session": "extended"' in audit.path.read_text()


def test_the_policy_defaults_fill_in_what_the_request_leaves_out(tmp_path):
    send = Fake()
    policy = t.Policy(**{**POLICY.__dict__, "default_session": "extended", "default_tif": "gtc"})
    trader = t.Trader(
        policy, {"schwab": lambda: t.SchwabTrader("tok", send)}, t.Audit(tmp_path / "a.jsonl")
    )
    pv = trader.preview(order())  # no session or tif given
    assert (pv["order"]["session"], pv["order"]["tif"]) == ("extended", "gtc")
    trader.confirm(pv["preview_id"], "BUY AAPL")
    assert body_of(send)["session"] == "SEAMLESS"
    pv = trader.preview(order(session="normal"))  # an explicit choice wins
    assert pv["order"]["session"] == "normal"


def test_a_market_order_cannot_use_extended_hours_and_bad_values_are_refused(rig):
    trader, *_ = rig
    with pytest.raises(t.TradingError, match="market order can only trade in regular hours"):
        trader.preview(order(kind="market", price=None, session="extended"))
    with pytest.raises(t.TradingError, match="cannot rest"):
        trader.preview(order(kind="market", price=None, session="normal", tif="gtc"))
    with pytest.raises(t.TradingError, match="hours must be"):
        trader.preview(order(session="overnight"))
    with pytest.raises(t.TradingError, match="duration must be"):
        trader.preview(order(tif="forever"))


def test_the_policy_reads_and_checks_the_default_hours_and_duration(tmp_path):
    path = tmp_path / "p.toml"
    path.write_text('enabled = true\n[defaults]\nsession = "extended"\ntime_in_force = "day"\n')
    os.chmod(path, 0o600)
    policy = t.load_policy(path)
    assert (policy.default_session, policy.default_tif) == ("extended", "day")
    path.write_text('[defaults]\nsession = "moon"\n')
    with pytest.raises(t.TradingError, match="session is normal or extended"):
        t.load_policy(path)


@pytest.mark.parametrize(
    "tif, session, expected",
    [
        ("day", "normal", ("DAY", "NORMAL")),
        ("day", "extended", ("DAY", "SEAMLESS")),
        ("gtc", "normal", ("GOOD_TILL_CANCEL", "NORMAL")),
        ("gtc", "extended", ("GOOD_TILL_CANCEL", "SEAMLESS")),
        ("day", "am", ("DAY", "AM")),
        ("gtc", "pm", ("GOOD_TILL_CANCEL", "PM")),
        ("fok", "normal", ("FILL_OR_KILL", "NORMAL")),
        ("ioc", "normal", ("IMMEDIATE_OR_CANCEL", "NORMAL")),
        ("end_of_week", "normal", ("END_OF_WEEK", "NORMAL")),
        ("end_of_month", "normal", ("END_OF_MONTH", "NORMAL")),
        ("next_end_of_month", "normal", ("NEXT_END_OF_MONTH", "NORMAL")),
    ],
)
def test_every_schwab_duration_and_session_reaches_the_broker_as_asked(rig, tif, session, expected):
    trader, send, *_ = rig
    pv = trader.preview(order(quantity=1, tif=tif, session=session))
    trader.confirm(pv["preview_id"], "BUY AAPL")
    sent = body_of(send)
    assert (sent["duration"], sent["session"]) == expected


@pytest.mark.parametrize("tif", ["fok", "ioc", "end_of_week", "end_of_month", "next_end_of_month"])
@pytest.mark.parametrize("session", ["extended", "am", "pm"])
def test_only_day_and_gtc_orders_can_use_the_extended_pre_market_or_after_hours_sessions(
    rig, tif, session
):
    trader, *_ = rig
    with pytest.raises(t.TradingError, match="can be day or good till cancelled"):
        trader.preview(order(quantity=1, tif=tif, session=session))


def test_a_market_order_defaults_to_a_regular_hours_day_order_and_may_be_fill_or_kill(rig):
    trader, send, *_ = rig
    pv = trader.preview(order(kind="market", price=None, quantity=1))
    assert (pv["order"]["session"], pv["order"]["tif"]) == ("normal", "day")
    pv = trader.preview(order(kind="market", price=None, quantity=1, tif="fok"))
    trader.confirm(pv["preview_id"], "BUY AAPL")
    assert body_of(send)["duration"] == "FILL_OR_KILL"


def test_the_default_templates_are_one_two_four_and_eight_percent_under_for_buys_and_over_for_sells(
    rig,
):
    trader, *_ = rig
    got = {x["name"]: x for x in trader.quote("AAPL")["templates"]}
    assert [x["price"] for x in got.values() if x["side"] == "buy"] == [99.0, 98.0, 96.0, 92.0]
    assert [x["price"] for x in got.values() if x["side"] == "sell"] == [101.0, 102.0, 104.0, 108.0]
    assert got["Buy 4% under"]["offset_pct"] == -4 and got["Sell 8% over"]["side"] == "sell"


def test_binance_template_prices_land_on_the_tick(rig):
    trader, *_ = rig
    prices = [x["price"] for x in trader.quote("BTC-USD")["templates"] if x["side"] == "buy"]
    assert prices == [49500.0, 49000.0, 48000.0, 46000.0]


def test_the_policy_can_define_its_own_templates_with_a_duration_and_hours(tmp_path):
    path = tmp_path / "p.toml"
    path.write_text(
        'enabled = true\n[[templates]]\nname = "Dip 3%"\nside = "buy"\noffset_pct = -3\ntif = "gtc"\nsession = "extended"\n'
        '[[templates]]\nname = "Take 6%"\nside = "sell"\noffset_pct = 6\n'
    )
    os.chmod(path, 0o600)
    assert t.load_policy(path).templates == (
        ("Dip 3%", "buy", -3.0, "gtc", "extended"),
        ("Take 6%", "sell", 6.0, "", ""),
    )
    for bad in (
        '[[templates]]\nname = "x"\nside = "hold"\noffset_pct = 1\n',
        '[[templates]]\nname = "x"\nside = "buy"\noffset_pct = 99\n',
        '[[templates]]\nname = "x"\nside = "buy"\noffset_pct = 1\ntif = "forever"\n',
        '[[templates]]\nside = "buy"\noffset_pct = 1\n',
    ):
        path.write_text("enabled = true\n" + bad)
        with pytest.raises(t.TradingError):
            t.load_policy(path)
    path.write_text("enabled = true\n")
    assert t.load_policy(path).templates is None  # none given: the built-in set applies


def test_an_empty_templates_list_means_no_templates_not_the_defaults(rig):
    trader, *_ = rig
    trader.policy = t.Policy(**{**POLICY.__dict__, "templates": ()})
    assert trader.quote("AAPL")["templates"] == []


ACCOUNTS = {
    "accounts": [
        {
            "broker": "schwab",
            "id": "1234",
            "label": "Schwab …1234",
            "total_value": 10000.0,
            "cash": 2000.0,
            "spendable": 4000.0,
            "buying_power": 4500.0,
            "holdings": {"AAPL": {"quantity": 5.0, "market_value": 500.0}},
        },
        {
            "broker": "schwab",
            "id": "77",
            "label": "Schwab …77",
            "total_value": 30000.0,
            "cash": 500.0,
            "spendable": 500.0,
            "buying_power": 500.0,
            "holdings": {"AAPL": {"quantity": 10.0, "market_value": 1000.0}},
        },
        {
            "broker": "binance",
            "id": "binance",
            "label": "Binance.US spot",
            "total_value": 60000.0,
            "cash": 100.0,
            "spendable": 100.0,
            "buying_power": 100.0,
            "holdings": {"BTC-USD": {"quantity": 1.0, "market_value": 50000.0}},
        },
    ]
}


def test_a_preview_shows_how_the_order_changes_the_position_in_the_account_and_overall(rig):
    trader, *_ = rig
    trader.accounts_loader = lambda: ACCOUNTS
    pv = trader.preview(order(quantity=2, price=100))  # buy $200 of AAPL in account 1234
    imp = pv["impact"]
    assert imp["cost"] == 200 and imp["account"]["label"] == "Schwab …1234"
    assert imp["account"]["quantity"] == {"before": 5.0, "after": 7.0}
    assert imp["account"]["value"] == {"before": 500.0, "after": 700.0}
    assert imp["account"]["percent"]["before"] == pytest.approx(5.0)
    assert imp["account"]["percent"]["after"] == pytest.approx(7.0)
    assert imp["account"]["cash_after"] == 1800.0 and imp["account"]["buying_power_after"] == 3800.0
    assert imp["overall"]["value"] == {
        "before": 1500.0,
        "after": 1700.0,
    }  # both Schwab accounts' AAPL
    total = 10000 + 30000 + 60000
    assert round(imp["overall"]["percent"]["before"], 4) == round(1500 / total * 100, 4)
    assert round(imp["overall"]["percent"]["after"], 4) == round(1700 / total * 100, 4)


def test_a_sell_reduces_the_position_and_adds_to_cash_and_binance_uses_its_own_account(rig):
    trader, *_ = rig
    trader.accounts_loader = lambda: ACCOUNTS
    sell = trader.preview(order(side="sell", quantity=3, price=100))["impact"]
    assert sell["account"]["value"] == {"before": 500.0, "after": 200.0}
    assert sell["account"]["cash_after"] == 2300.0
    btc = trader.preview(order(ticker="BTC-USD", account="", quantity=0.01, price=50000))["impact"]
    assert (
        btc["account"]["label"] == "Binance.US spot" and btc["account"]["value"]["after"] == 50500.0
    )


def test_the_preview_still_works_when_account_details_are_unavailable(rig):
    trader, *_ = rig
    assert trader.preview(order())["impact"] is None  # no loader

    def boom():
        raise OSError("broker down")

    trader.accounts_loader = boom
    assert trader.preview(order())["impact"] is None


def test_button_confirmation_needs_no_phrase_but_the_preview_is_still_single_use_and_expiring(rig):
    trader, send, _, clock = rig
    trader.policy = t.Policy(**{**POLICY.__dict__, "confirmation": "button"})
    pv = trader.preview(order())
    assert pv["confirmation"] == "button"
    assert trader.confirm(pv["preview_id"], "")["placed"] is True
    with pytest.raises(t.TradingError, match="expired or was already used"):
        trader.confirm(pv["preview_id"], "")
    pv = trader.preview(order())
    clock["now"] += t.PREVIEW_TTL_SECONDS + 1
    with pytest.raises(t.TradingError, match="expired"):
        trader.confirm(pv["preview_id"], "")
    assert len(placements(send)) == 1


def test_phrase_confirmation_is_the_default_and_a_cancel_always_needs_its_phrase(rig):
    trader, *_ = rig
    assert POLICY.confirmation == "phrase"
    trader.policy = t.Policy(**{**POLICY.__dict__, "confirmation": "button"})
    with pytest.raises(t.TradingError, match="CANCEL 777"):
        trader.cancel("schwab", "777", "", account="1234")


def test_the_policy_reads_the_confirmation_mode(tmp_path):
    path = tmp_path / "p.toml"
    path.write_text('enabled = true\n[defaults]\nconfirmation = "button"\n')
    os.chmod(path, 0o600)
    assert t.load_policy(path).confirmation == "button"
    path.write_text('[defaults]\nconfirmation = "none"\n')
    with pytest.raises(t.TradingError, match="confirmation is phrase or button"):
        t.load_policy(path)


def test_schwab_validation_text_is_read_from_activity_message():
    def reject(method, url, headers, body):
        if url.endswith("previewOrder"):
            return (
                200,
                {},
                {
                    "orderValidationResult": {
                        "rejects": [
                            {
                                "activityMessage": "You do not have enough available cash/buying power for this order."
                            }
                        ]
                    }
                },
            )
        return Fake()(method, url, headers, body)

    broker = t.SchwabTrader("tok", reject)
    with pytest.raises(t.TradingError, match="not have enough available cash/buying power"):
        broker.preview(t.Order("AAPL", "buy", "limit", 1.0, 100.0, "1234"))

    def warn(method, url, headers, body):
        if url.endswith("previewOrder"):
            return (
                200,
                {},
                {
                    "orderValidationResult": {
                        "warns": [{"activityMessage": "extended hours risk"}],
                        "alerts": [{"message": "old style"}],
                    }
                },
            )
        return Fake()(method, url, headers, body)

    assert t.SchwabTrader("tok", warn).preview(t.Order("AAPL", "buy", "limit", 1.0, 100.0, "1234"))[
        "warnings"
    ] == ["extended hours risk", "old style"]


def test_the_impact_also_counts_institutions_read_from_statements_such_as_fidelity(rig):
    trader, *_ = rig
    trader.accounts_loader = lambda: ACCOUNTS
    trader.statements_loader = lambda: {
        "institutions": {"fid": {"name": "Fidelity", "total": 20000.0, "as_of": "2026-02-28"}},
        "holdings": {"AAPL": {"quantity": 4.0, "market_value": 400.0}},
    }
    imp = trader.preview(order(quantity=2, price=100))["impact"]
    ev = imp["everything"]
    assert ev["value"] == {
        "before": 1900.0,
        "after": 2100.0,
    }  # both Schwab accounts' AAPL plus Fidelity's
    assert ev["total"] == 100000 + 20000 and ev["institutions"][0]["as_of"] == "2026-02-28"
    assert ev["percent"]["after"] == pytest.approx(2100 / 120000 * 100)
    assert imp["overall"]["total"] == 100000  # the live-only figure is unchanged
    trader.statements_loader = lambda: {}
    assert trader.preview(order(quantity=2, price=100))["impact"]["everything"] is None


def test_the_preview_lists_the_symbol_in_every_account_with_the_chosen_one_before_and_after(rig):
    trader, *_ = rig
    trader.accounts_loader = lambda: ACCOUNTS
    rows = trader.preview(order(quantity=2, price=100))["impact"]["holdings"]
    by = {r["id"]: r for r in rows}
    assert (
        by["1234"]["quantity"] == 5.0
        and by["1234"]["quantity_after"] == 7.0
        and by["1234"]["chosen"] is True
    )
    assert by["1234"]["value_after"] == 700.0 and by["1234"]["percent"] == pytest.approx(5.0)
    assert by["77"]["quantity"] == 10.0 and "chosen" not in by["77"]  # other accounts are unchanged
    assert by["binance"]["quantity"] == 0.0


def test_holdings_for_a_ticker_lists_each_account_and_the_statement_institutions(rig):
    trader, *_ = rig
    trader.accounts_loader = lambda: ACCOUNTS
    trader.statements_loader = lambda: {
        "institutions": {"fid": {"name": "Fidelity", "total": 9.0, "as_of": "2026-02-28"}},
        "holdings": {"AAPL": {"quantity": 4.0, "market_value": 400.0}},
    }
    got = trader.holdings_for("aapl")
    assert [(r["id"], r["quantity"]) for r in got["accounts"]] == [
        ("1234", 5.0),
        ("77", 10.0),
        ("binance", 0.0),
    ]
    assert got["statements"] == [
        {"name": "Fidelity", "as_of": "2026-02-28", "quantity": 4.0, "market_value": 400.0}
    ]
    assert trader.holdings_for("MSFT")["statements"] == []
    with pytest.raises(t.TradingError):
        trader.holdings_for("not a ticker!")


def test_the_service_serves_holdings_per_ticker_behind_the_same_gate(tmp_path):
    from tradility import trader_service as ts

    svc = ts.Service(lambda: POLICY, t.Audit(tmp_path / "a.jsonl"), send=Fake())
    svc.account_loader = lambda: ACCOUNTS
    good = {"Host": "127.0.0.1:8768", "X-Tradility-Trader": "1"}
    code, body = svc.handle("GET", "/holdings/AAPL", good, b"")
    assert code == 200 and body["symbol"] == "AAPL" and len(body["accounts"]) == 3
    assert (
        svc.handle(
            "GET", "/holdings/AAPL", {"Host": "evil.example", "X-Tradility-Trader": "1"}, b""
        )[0]
        == 403
    )


FID_STATEMENTS = {
    "institutions": {"fid": {"name": "Fidelity", "total": 5000.0, "as_of": "2026-02-28"}},
    "holdings": {"AAPL": {"quantity": 4.0, "market_value": 400.0}},
    "accounts": [
        {
            "broker": "fidelity",
            "id": "4917",
            "key": "4917:individ",
            "label": "Fidelity …4917",
            "type": "individ",
            "total_value": 5000.0,
            "as_of": "2026-02-28",
            "holdings": {"AAPL": {"quantity": 4.0, "market_value": 400.0}},
        },
        {
            "broker": "fidelity",
            "id": "7866",
            "key": "7866:bl-trad",
            "label": "Fidelity …7866",
            "type": "bl-trad",
            "total_value": 40000.0,
            "as_of": "2026-02-28",
            "holdings": {},
        },
        {
            "broker": "fidelity",
            "id": "7866",
            "key": "7866:hsa",
            "label": "Fidelity …7866",
            "type": "hsa",
            "total_value": 281.0,
            "as_of": "2026-02-28",
            "holdings": {},
        },
    ],
}


@pytest.fixture
def fid(rig):
    trader, send, audit, clock = rig
    trader.policy = t.Policy(**{**POLICY.__dict__, "fidelity_accounts": ("4917", "7866")})
    trader.accounts_loader = lambda: ACCOUNTS
    trader.statements_loader = lambda: FID_STATEMENTS
    return trader, send, audit


def test_a_fidelity_ticket_lays_the_order_out_in_fidelitys_terms_and_sends_nothing(fid):
    trader, send, audit = fid
    got = trader.fidelity_ticket(
        order(account="4917", quantity=2, price=100, tif="day", session="normal")
    )
    fields = dict(got["fields"])
    assert fields["Action"] == "Buy" and fields["Symbol"] == "AAPL" and fields["Quantity"] == "2"
    assert fields["Order type"] == "Limit" and fields["Limit price"] == "$100.00"
    assert fields["Time in force"] == "Day" and fields["Extended hours"] == "No"
    assert "Symbol: AAPL" in got["text"] and got["url"].startswith("https://digital.fidelity.com/")
    assert not placements(send) and not any(
        c[0] == "POST" for c in send.calls
    )  # nothing goes anywhere
    assert any("2026-02-28 statement" in w for w in got["warnings"])
    events = [json.loads(line)["event"] for line in audit.path.read_text().splitlines()]
    assert (
        events == ["fidelity_ticket"] and audit.spent_today() == 0
    )  # a ticket is not a sent order


def test_the_ticket_shows_the_impact_in_the_fidelity_account_from_its_statement(fid):
    trader, *_ = fid
    imp = trader.fidelity_ticket(order(account="4917", quantity=2, price=100))["impact"]
    assert imp["account"]["label"] == "Fidelity …4917"
    assert imp["account"]["quantity"] == {"before": 4.0, "after": 6.0}
    assert imp["account"]["value"] == {"before": 400.0, "after": 600.0}
    assert imp["account"]["percent"]["before"] == pytest.approx(8.0)  # 400 / 5000
    ids = [r["id"] for r in imp["holdings"]]
    assert "4917" in ids and "1234" in ids  # Schwab accounts are in the picture too


def test_a_shared_mask_is_told_apart_by_the_account_type(fid):
    trader, *_ = fid
    assert (
        trader._fidelity_account("7866")["type"] == "bl-trad"
    )  # the larger of the two with that mask
    assert trader._fidelity_account("7866:hsa")["total_value"] == 281.0
    accounts = trader.fidelity_accounts()
    assert [a["id"] for a in accounts] == ["4917", "7866"] and accounts[0]["as_of"] == "2026-02-28"


@pytest.mark.parametrize(
    "change, message",
    [
        ({"account": "9999"}, "Fidelity accounts the policy lists"),
        ({"account": ""}, "Fidelity accounts the policy lists"),
        ({"ticker": "BTC-USD"}, "crypto"),
        ({"tif": "fok"}, "day or good till canceled"),
        ({"session": "extended", "tif": "gtc"}, "extended-hours orders are day limit orders"),
        ({"quantity": 1.5}, "whole shares"),
        ({"quantity": 11}, "per-order limit"),
        ({"price": 130}, "away from the last price"),
        ({"side": "short"}, "buy or sell"),
    ],
)
def test_fidelity_tickets_obey_the_policy_and_fidelitys_own_rules(fid, change, message):
    trader, send, _ = fid
    with pytest.raises(t.TradingError, match=message):
        trader.fidelity_ticket(order(**{"account": "4917", **change}))
    assert not placements(send)


def test_a_gtc_ticket_warns_about_the_sixty_day_limit_and_tickets_need_trading_enabled(fid):
    trader, *_ = fid
    got = trader.fidelity_ticket(order(account="4917", tif="gtc", session="normal"))
    assert (
        any("60 days" in w for w in got["warnings"])
        and dict(got["fields"])["Time in force"] == "Good till canceled"
    )
    trader.policy = t.Policy(**{**trader.policy.__dict__, "enabled": False})
    with pytest.raises(t.TradingError, match="turned off"):
        trader.fidelity_ticket(order(account="4917"))


def test_the_service_serves_fidelity_tickets_and_lists_the_accounts_in_its_status(
    tmp_path, monkeypatch
):
    from tradility import trader_service as ts

    monkeypatch.setenv(
        "TRADILITY_SCHWAB_ACCESS_TOKEN", "tok"
    )  # the live price comes from Schwab's quote

    policy = t.Policy(**{**POLICY.__dict__, "fidelity_accounts": ("4917",)})
    svc = ts.Service(lambda: policy, t.Audit(tmp_path / "a.jsonl"), send=Fake())
    svc.account_loader = lambda: ACCOUNTS
    svc._statements = lambda: FID_STATEMENTS
    svc.trader.statements_loader = svc._statements
    good = {"Host": "127.0.0.1:8768", "X-Tradility-Trader": "1"}
    code, status = svc.handle("GET", "/status", good, b"")
    assert status["fidelity_accounts"][0]["label"] == "Fidelity …4917"
    body = json.dumps(order(account="4917", quantity=1, price=100)).encode()
    code, ticket = svc.handle("POST", "/fidelity/ticket", good, body)
    assert code == 200 and ticket["venue"] == "fidelity" and ticket["fields"][0][0] == "Account"
    assert svc.handle("POST", "/fidelity/ticket", {"Host": "evil.example"}, body)[0] == 403


def test_quotes_fetch_many_tickers_in_few_requests_and_skip_unpriced_ones(tmp_path):
    urls = []

    def send(method, url, headers, body):
        urls.append(url)
        if "marketdata/v1/quotes" in url:
            return (
                200,
                {},
                {
                    "AAA": {"quote": {"mark": 12.5, "lastPrice": 12.0}},
                    "FUND": {"quote": {"nAV": 30.25}},
                    "BAD": {"quote": {"mark": 0}},
                },
            )
        if url.endswith("/api/v3/ticker/price"):
            return (
                200,
                {},
                [{"symbol": "BTCUSD", "price": "60000"}, {"symbol": "XXXUSD", "price": "1"}],
            )
        return 404, {}, {}

    open_policy = dataclasses.replace(POLICY, tickers=frozenset({"*"}))
    trader = t.Trader(
        open_policy,
        {
            "schwab": lambda: t.SchwabTrader("tok", send),
            "binance": lambda: t.BinanceTrader("KEY", "SECRET", send, clock=lambda: 1.0),
        },
        t.Audit(tmp_path / "a.jsonl"),
    )
    result = trader.quotes(["aaa", "FUND", "BAD", "BTC-USD", "NOPE-USD", "aaa"])
    assert result["quotes"] == {"AAA": 12.5, "FUND": 30.25, "BTC-USD": 60000.0}
    assert result["requested"] == 5
    assert sum("marketdata/v1/quotes" in u for u in urls) == 1  # all stocks in one request
    assert sum(u.endswith("/api/v3/ticker/price") for u in urls) == 1  # all coins in one request


def test_coin_prices_are_shown_even_when_binance_trading_is_off(tmp_path, monkeypatch):
    def public_send(method, url, headers, body):
        assert headers == {} and "SIGN" not in url  # public data: no key, no signature
        return 200, {}, [{"symbol": "ETHUSD", "price": "2500"}]

    monkeypatch.setattr(t, "http_send", public_send)
    schwab_only = dataclasses.replace(POLICY, brokers=("schwab",), tickers=frozenset({"*"}))
    trader = t.Trader(
        schwab_only,
        {"schwab": lambda: t.SchwabTrader("tok", lambda *a: (200, {}, {}))},
        t.Audit(tmp_path / "a.jsonl"),
    )
    assert trader.quotes(["ETH-USD"])["quotes"] == {"ETH-USD": 2500.0}
