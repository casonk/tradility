"""Read-only account details: normalisation, valuation, failure handling and the service endpoint."""

import json

from tradility import accounts as a
from tradility import trader_service as ts

SCHWAB = {
    "securitiesAccount": {
        "type": "MARGIN",
        "accountNumber": "99996427",
        "isDayTrader": False,
        "isClosingOnlyRestricted": False,
        "positions": [{}, {}, {}],
        "initialBalances": {"cashAvailableForTrading": 1500.5, "maintenanceCall": 0},
        "currentBalances": {
            "liquidationValue": 25000.0,
            "cashBalance": 1800.25,
            "buyingPower": 40000.0,
            "availableFunds": 20000.0,
            "dayTradingBuyingPower": 80000.0,
            "marginBalance": 0.0,
            "longMarketValue": 23200.0,
            "equityPercentage": 100.0,
            "maintenanceCall": 0.0,
            "regTCall": 0.0,
        },
        "projectedBalances": {"isInCall": False},
    }
}


def test_a_schwab_account_is_flattened_with_only_the_last_four_digits():
    got = a.schwab_account(SCHWAB)
    assert (
        got["id"] == "6427" and got["label"] == "Schwab …6427" and "99996427" not in json.dumps(got)
    )
    assert got["type"] == "MARGIN" and got["margin_eligible"] is True
    assert (got["total_value"], got["cash"], got["buying_power"], got["available_funds"]) == (
        25000.0,
        1800.25,
        40000.0,
        20000.0,
    )
    assert got["cash_available_to_trade"] == 1500.5 and got["day_trading_buying_power"] == 80000.0
    assert got["positions"] == 3 and got["in_call"] is False and got["day_trader"] is False


def test_a_cash_account_has_no_margin_figures_and_a_call_is_flagged():
    cash = json.loads(json.dumps(SCHWAB))
    cash["securitiesAccount"]["type"] = "CASH"
    cash["securitiesAccount"]["currentBalances"]["maintenanceCall"] = 120.0
    got = a.schwab_account(cash)
    assert (
        got["margin_eligible"] is False
        and got["margin_balance"] is None
        and got["day_trading_buying_power"] is None
    )
    assert got["in_call"] is True


def test_binance_balances_are_valued_in_dollars_and_dust_and_unpriced_assets_are_left_out():
    account = {
        "accountType": "SPOT",
        "canTrade": True,
        "canWithdraw": True,
        "balances": [
            {"asset": "USD", "free": "100.5", "locked": "0"},
            {"asset": "USDT", "free": "20", "locked": "0"},
            {"asset": "BTC", "free": "0.5", "locked": "0.5"},
            {"asset": "DOGE", "free": "0.0001", "locked": "0"},  # dust
            {"asset": "RARE", "free": "5", "locked": "0"},  # no USD price
        ],
    }
    got = a.binance_account(account, {"BTC": 50000.0, "DOGE": 0.1})
    assert got["total_value"] == 100.5 + 20 + 50000.0
    assert got["cash"] == 100.5 and got["positions"] == 3 and got["can_withdraw"] is True


def test_a_failing_source_is_reported_by_class_only_and_the_other_still_returns():
    def get(url, headers):
        if "schwabapi" in url:
            raise OSError("secret-token-value leaked in a message")
        if url.endswith("/ticker/price"):
            return [{"symbol": "BTCUSD", "price": "10"}]
        return {"accountType": "SPOT", "balances": [{"asset": "BTC", "free": "1", "locked": "0"}]}

    env = {
        "TRADILITY_SCHWAB_ACCESS_TOKEN": "secret-token-value",
        "TRADILITY_BINANCE_API_KEY": "k",
        "TRADILITY_BINANCE_API_SECRET": "s",
    }
    got = a.collect(get, env)
    assert got["sources"]["schwab"] == {"ok": False, "error": "request failed: OSError"}
    assert got["accounts"][0]["total_value"] == 10.0
    assert "secret-token-value" not in json.dumps(got)


def test_the_service_serves_accounts_cached_and_behind_the_same_gate():
    calls = []
    svc = ts.Service(lambda: ts.t.Policy())
    svc.account_loader = lambda: calls.append(1) or {"accounts": [{"id": "6427"}], "sources": {}}
    good = {"Host": "127.0.0.1:8768", "X-Tradility-Trader": "1"}
    assert svc.handle("GET", "/accounts", good, b"")[1]["accounts"] == [{"id": "6427"}]
    svc.handle("GET", "/accounts", good, b"")
    assert len(calls) == 1  # the second request within the TTL is served from the cache
    assert (
        svc.handle("GET", "/accounts", {"Host": "evil.example", "X-Tradility-Trader": "1"}, b"")[0]
        == 403
    )


def test_this_module_only_reads():
    import re
    from pathlib import Path

    assert not re.search(
        r"method\s*=|\.post\(|\bPOST\b|\bDELETE\b|\bPUT\b", Path(a.__file__).read_text()
    )
