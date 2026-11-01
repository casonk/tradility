"""Deposit/withdrawal history (read-only) and Binance.US withdrawals (allowlist, caps, typed phrase)."""

import json
from datetime import UTC, datetime

import pytest

from tradility import trading as t
from tradility import transfers as tr

NOW = datetime(2026, 11, 1, tzinfo=UTC)


def test_schwab_cash_movements_are_normalised_with_direction_and_a_short_note():
    got = tr.schwab_transfer(
        {
            "netAmount": -250.0,
            "tradeDate": "2026-10-05T04:00:00+0000",
            "type": "ACH_DISBURSEMENT",
            "status": "VALID",
            "description": "x" * 200,
        },
        "6427",
    )
    assert got["direction"] == "out" and got["amount"] == 250.0 and got["date"] == "2026-10-05"
    assert got["kind"] == "ach disbursement" and got["account"] == "6427" and len(got["note"]) == 80
    assert tr.schwab_transfer({"netAmount": None}, "1") is None


def test_binance_deposits_and_withdrawals_hide_all_but_the_end_of_an_address():
    rows = tr.binance_transfers(
        [
            {
                "coin": "BTC",
                "amount": "0.5",
                "insertTime": 1790000000000,
                "network": "BTC",
                "status": 1,
            }
        ],
        [
            {
                "coin": "ETH",
                "amount": "2",
                "applyTime": "2026-09-01 10:00:00",
                "network": "ETH",
                "status": 6,
                "address": "0xABCDEFGHIJKLMNOP123456",
            }
        ],
    )
    dep, wd = rows
    assert (dep["direction"], dep["status"]) == ("in", "credited")
    assert (wd["direction"], wd["status"], wd["note"]) == ("out", "completed", "to …123456")
    assert "ABCDEFGHIJKLMNOP" not in json.dumps(rows)


def test_transfer_history_is_get_only_and_a_failing_source_is_named_by_class():
    import re
    from pathlib import Path

    assert not re.search(
        r"method\s*=|\.post\(|\bPOST\b|\bDELETE\b|\bPUT\b", Path(tr.__file__).read_text()
    )

    def get(url, headers):
        if "schwabapi" in url:
            raise OSError("secret-token-value")
        return []

    got = tr.collect(
        get,
        {
            "TRADILITY_SCHWAB_ACCESS_TOKEN": "secret-token-value",
            "TRADILITY_BINANCE_API_KEY": "k",
            "TRADILITY_BINANCE_API_SECRET": "s",
        },
        NOW,
    )
    assert got["sources"]["schwab"] == {"ok": False, "error": "request failed: OSError"}
    assert got["sources"]["binance"]["ok"] and "secret-token-value" not in json.dumps(got)


ADDR = "0xDEADBEEF000000000000000000000000000ABC123"
POLICY = t.Policy(
    enabled=True,
    withdrawals_enabled=True,
    withdraw_per_usd=1000,
    withdraw_daily_usd=1500,
    withdraw_addresses=(("cold wallet", "ETH", "ETH", ADDR),),
)


class Fake:
    def __init__(self):
        self.calls, self.balance, self.fail = [], 10.0, None

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body))
        if "ticker/price" in url:
            return 200, {}, {"price": "500"}
        if "/api/v3/account" in url:
            return (
                200,
                {},
                {"balances": [{"asset": "ETH", "free": str(self.balance), "locked": "0"}]},
            )
        if "withdraw/apply" in url:
            if self.fail:
                raise self.fail
            return 200, {}, {"id": "w123"}
        return 404, {}, {"msg": url}


@pytest.fixture
def rig(tmp_path):
    send = Fake()
    brokers = {
        "binance_withdraw": lambda: t.BinanceWithdrawer("KEY", "SECRET", send, clock=lambda: 1.0)
    }
    audit = t.Audit(tmp_path / "audit.jsonl")
    return t.Trader(POLICY, brokers, audit), send, audit


def sent(send):
    return [c for c in send.calls if "withdraw/apply" in c[1]]


def test_a_withdrawal_goes_only_to_an_allowlisted_address_after_preview_and_the_typed_phrase(rig):
    trader, send, audit = rig
    pv = trader.withdraw_preview({"label": "cold wallet", "amount": 1})
    assert (
        pv["phrase"] == "WITHDRAW 1 ETH TO cold wallet"
        and pv["to"] == "…ABC123"
        and pv["notional"] == 500
    )
    assert ADDR not in json.dumps(pv) and not sent(
        send
    )  # a preview shows only the tail and sends nothing
    done = trader.withdraw_confirm(pv["preview_id"], "WITHDRAW 1 ETH TO cold wallet")
    assert done["withdrawn"] and done["withdrawal_id"] == "w123"
    body = sent(send)[0][2].decode()
    assert (
        f"address={ADDR}" in body
        and "coin=ETH" in body
        and "network=ETH" in body
        and "signature=" in body
    )
    events = [json.loads(line)["event"] for line in audit.path.read_text().splitlines()]
    assert events == ["withdraw_preview", "withdraw_sending", "withdrawn"]
    assert ADDR not in audit.path.read_text() and "SECRET" not in audit.path.read_text()


def test_an_unlisted_destination_a_wrong_phrase_a_reuse_or_too_little_balance_send_nothing(rig):
    trader, send, _ = rig
    with pytest.raises(t.TradingError, match="allowlist"):
        trader.withdraw_preview({"label": "someone else", "amount": 1})
    pv = trader.withdraw_preview({"label": "cold wallet", "amount": 1})
    with pytest.raises(t.TradingError, match="type exactly"):
        trader.withdraw_confirm(pv["preview_id"], "withdraw")
    with pytest.raises(t.TradingError, match="expired or was already used"):
        trader.withdraw_confirm(pv["preview_id"], "WITHDRAW 1 ETH TO cold wallet")
    send.balance = 0.5
    with pytest.raises(t.TradingError, match="not enough ETH"):
        trader.withdraw_preview({"label": "cold wallet", "amount": 1})
    assert not sent(send)


def test_the_per_withdrawal_and_daily_caps_count_unknown_outcomes_too(rig):
    trader, send, audit = rig
    with pytest.raises(t.TradingError, match="per-withdrawal limit"):
        trader.withdraw_preview({"label": "cold wallet", "amount": 3})  # 3 x $500 > $1000
    pv = trader.withdraw_preview({"label": "cold wallet", "amount": 2})  # exactly $1000
    send.fail = TimeoutError("slow")
    with pytest.raises(t.TradingError, match="outcome is unknown"):
        trader.withdraw_confirm(pv["preview_id"], pv["phrase"])
    assert len(sent(send)) == 1  # one attempt, never retried
    with pytest.raises(t.TradingError, match="daily limit"):
        trader.withdraw_preview(
            {"label": "cold wallet", "amount": 1.5}
        )  # $1000 unknown + $750 > $1500


def test_withdrawals_are_off_by_default_and_need_their_own_switch_and_credentials(tmp_path):
    send = Fake()
    brokers = {"binance_withdraw": lambda: t.BinanceWithdrawer("K", "S", send)}
    off = t.Policy(**{**POLICY.__dict__, "withdrawals_enabled": False})
    with pytest.raises(t.TradingError, match="turned off"):
        t.Trader(off, brokers, t.Audit(tmp_path / "a.jsonl")).withdraw_preview(
            {"label": "cold wallet", "amount": 1}
        )
    with pytest.raises(t.TradingError, match="no Binance.US withdrawal credentials"):
        t.Trader(POLICY, {}, t.Audit(tmp_path / "b.jsonl")).withdraw_preview(
            {"label": "cold wallet", "amount": 1}
        )
    assert not send.calls


def test_an_order_preview_cannot_be_confirmed_as_a_withdrawal_or_the_reverse(rig):
    trader, *_ = rig
    pv = trader.withdraw_preview({"label": "cold wallet", "amount": 1})
    with pytest.raises(t.TradingError, match="for a withdrawal"):
        trader.confirm(pv["preview_id"], "WITHDRAW 1 ETH TO cold wallet")


def test_the_policy_parses_withdrawals_and_the_service_lists_only_destination_tails(tmp_path):
    import os

    from tradility import trader_service as ts

    path = tmp_path / "trading.toml"
    path.write_text(
        f"enabled = true\n[withdrawals]\nenabled = true\nper_withdrawal_usd = 100\ndaily_withdrawal_usd = 200\n"
        f'[[withdrawals.addresses]]\nlabel = "cold wallet"\ncoin = "eth"\nnetwork = "ETH"\naddress = "{ADDR}"\n'
    )
    os.chmod(path, 0o600)
    policy = t.load_policy(path)
    assert policy.withdrawals_enabled and policy.withdraw_addresses == (
        ("cold wallet", "ETH", "ETH", ADDR),
    )
    svc = ts.Service(lambda: policy, t.Audit(tmp_path / "a.jsonl"))
    code, status = svc.handle(
        "GET", "/status", {"Host": "127.0.0.1:8768", "X-Tradility-Trader": "1"}, b""
    )
    assert status["withdrawals"]["destinations"] == [
        {"label": "cold wallet", "coin": "ETH", "network": "ETH", "to": "…ABC123"}
    ]
    assert ADDR not in json.dumps(status)
    path.write_text(
        "[withdrawals]\n[[withdrawals.addresses]]\nlabel = 'x'\n"
    )  # missing coin/address
    with pytest.raises(t.TradingError, match="bad value"):
        t.load_policy(path)


def test_the_service_serves_transfers_cached_and_routes_withdrawals_behind_the_same_gate(tmp_path):
    from tradility import trader_service as ts

    calls = []
    svc = ts.Service(lambda: POLICY, t.Audit(tmp_path / "a.jsonl"), send=Fake())
    svc.transfer_loader = lambda: calls.append(1) or {"transfers": [], "sources": {}}
    good = {"Host": "127.0.0.1:8768", "X-Tradility-Trader": "1"}
    assert svc.handle("GET", "/transfers", good, b"")[0] == 200
    svc.handle("GET", "/transfers", good, b"")
    assert len(calls) == 1
    assert (
        svc.handle("GET", "/transfers", {"Host": "evil.example", "X-Tradility-Trader": "1"}, b"")[0]
        == 403
    )
    code, refused = svc.handle(
        "POST", "/withdraw/preview", good, json.dumps({"label": "nobody", "amount": 1}).encode()
    )
    assert code == 422 and "allowlist" in refused["error"] or "credentials" in refused["error"]
    assert svc.handle("POST", "/withdraw/preview", {"Host": "evil.example"}, b"{}")[0] == 403
