"""Order placement: the policy fails closed, orders need a preview and a typed confirmation."""

import json
import os
from datetime import UTC, datetime

import pytest

from tradility import trading as t

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
