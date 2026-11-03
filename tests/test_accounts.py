"""Read-only account details: normalisation, valuation, failure handling and the service endpoint."""

import json

import pytest

from tradility import accounts as a
from tradility import trader_service as ts

SCHWAB = {
    "securitiesAccount": {
        "type": "MARGIN",
        "accountNumber": "99996427",
        "isDayTrader": False,
        "isClosingOnlyRestricted": False,
        "positions": [{}, {}, {}],
        # Schwab reports 0.0 here outside the start of the day; it must never become "available to trade"
        "initialBalances": {"cashAvailableForTrading": 0.0, "maintenanceCall": 0},
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
        "projectedBalances": {"isInCall": False, "stockBuyingPower": 38000.0},
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
    assert (
        got["spendable"] == 38000.0 and got["day_trading_buying_power"] == 80000.0
    )  # projected stock buying power
    assert "cash_available_to_trade" not in got
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


def test_holdings_are_collected_for_sizing_orders_and_stripped_from_the_page_feed(tmp_path):
    item = json.loads(json.dumps(SCHWAB))
    item["securitiesAccount"]["positions"] = [
        {
            "instrument": {"symbol": "aapl"},
            "longQuantity": 4,
            "shortQuantity": 0,
            "marketValue": 800.0,
        },
        {
            "instrument": {"symbol": "AAPL"},
            "longQuantity": 1,
            "shortQuantity": 0,
            "marketValue": 200.0,
        },
    ]
    assert a.schwab_account(item)["holdings"] == {
        "AAPL": {"quantity": 5.0, "market_value": 1000.0, "average_price": None}
    }
    got = a.binance_account(
        {
            "balances": [
                {"asset": "BTC", "free": "0.5", "locked": "0"},
                {"asset": "USD", "free": "10", "locked": "0"},
            ]
        },
        {"BTC": 100.0},
    )
    assert got["holdings"] == {"BTC-USD": {"quantity": 0.5, "market_value": 50.0}}
    svc = ts.Service(lambda: ts.t.Policy())
    svc.account_loader = lambda: {"accounts": [{"id": "x", "holdings": {"A": {}}}], "sources": {}}
    good = {"Host": "127.0.0.1:8768", "X-Tradility-Trader": "1"}
    assert "holdings" not in svc.handle("GET", "/accounts", good, b"")[1]["accounts"][0]
    assert svc.trader.accounts_loader()["accounts"][0]["holdings"] == {
        "A": {}
    }  # kept for the impact


def test_statement_institutions_are_read_from_the_holdings_export_and_live_ones_are_left_out(
    tmp_path,
):
    path = tmp_path / "holdings.json"
    path.write_text(
        json.dumps(
            {
                "holdings": [
                    {
                        "ticker": "AAPL",
                        "accounts": [
                            {
                                "institution": "cs",
                                "ending_value": 1000.0,
                                "quantity": 5,
                                "period_end": "2026-10-31",
                            },
                            {
                                "institution": "fid",
                                "ending_value": 400.0,
                                "quantity": 2,
                                "period_end": "2026-02-28",
                            },
                            {
                                "institution": "fid",
                                "ending_value": 100.0,
                                "quantity": 1,
                                "period_end": "2026-01-31",
                            },
                        ],
                    },
                    {
                        "ticker": "BTC",
                        "accounts": [
                            {
                                "institution": "bin",
                                "ending_value": 9000.0,
                                "quantity": 1,
                                "period_end": "2026-10-31",
                            }
                        ],
                    },
                    {
                        "ticker": "VTI",
                        "accounts": [
                            {
                                "institution": "fid",
                                "ending_value": 600.0,
                                "quantity": 3,
                                "period_end": "2026-02-28",
                            }
                        ],
                    },
                ]
            }
        )
    )
    got = a.statement_accounts(path)
    assert set(got["institutions"]) == {"fid"}
    assert got["institutions"]["fid"] == {
        "name": "Fidelity",
        "total": 1100.0,
        "as_of": "2026-02-28",
    }
    assert got["holdings"]["AAPL"] == {"quantity": 3.0, "market_value": 500.0}
    assert "BTC" not in got["holdings"]
    assert a.statement_accounts(tmp_path / "missing.json") == {} and a.statement_accounts("") == {}
    svc = ts.Service(lambda: ts.t.Policy())
    svc.account_loader = lambda: {"accounts": [], "sources": {}}
    import os

    os.environ["TRADILITY_HOLDINGS_FILE"] = str(path)
    try:
        good = {"Host": "127.0.0.1:8768", "X-Tradility-Trader": "1"}
        assert svc.handle("GET", "/accounts", good, b"")[1]["statements"][0]["name"] == "Fidelity"
    finally:
        del os.environ["TRADILITY_HOLDINGS_FILE"]


def test_spendable_falls_back_to_the_current_buying_power_and_is_never_the_initial_zero():
    bare = json.loads(json.dumps(SCHWAB))
    del bare["securitiesAccount"]["projectedBalances"]["stockBuyingPower"]
    assert a.schwab_account(bare)["spendable"] == 40000.0


def test_schwab_positions_carry_their_average_price_for_the_cost_of_a_position():
    item = json.loads(json.dumps(SCHWAB))
    item["securitiesAccount"]["positions"] = [
        {
            "instrument": {"symbol": "VWO"},
            "longQuantity": 4,
            "shortQuantity": 0,
            "marketValue": 222.0,
            "averagePrice": 58.67,
        },
        {
            "instrument": {"symbol": "NOPRICE"},
            "longQuantity": 1,
            "shortQuantity": 0,
            "marketValue": 9.0,
        },
    ]
    held = a.schwab_account(item)["holdings"]
    assert held["VWO"]["average_price"] == pytest.approx(58.67)
    assert held["NOPRICE"]["average_price"] is None


def test_statement_accounts_are_listed_one_by_one_with_their_holdings(tmp_path):
    path = tmp_path / "h.json"
    path.write_text(
        json.dumps(
            {
                "holdings": [
                    {
                        "ticker": "AAPL",
                        "accounts": [
                            {
                                "institution": "fid",
                                "account_mask": "***4917",
                                "account_subtype": "individ",
                                "account_name": "Fidelity Individual TOD",
                                "ending_value": 400.0,
                                "quantity": 2,
                                "period_end": "2026-02-28",
                            }
                        ],
                    },
                    {
                        "ticker": "VTI",
                        "accounts": [
                            {
                                "institution": "fid",
                                "account_mask": "***4917",
                                "account_subtype": "individ",
                                "ending_value": 600.0,
                                "quantity": 3,
                                "period_end": "2026-02-28",
                            },
                            {
                                "institution": "fid",
                                "account_mask": "***7866",
                                "account_subtype": "hsa",
                                "ending_value": 9.0,
                                "quantity": 1,
                                "period_end": "2026-02-28",
                            },
                        ],
                    },
                ]
            }
        )
    )
    got = a.statement_accounts(path)
    by = {x["key"]: x for x in got["accounts"]}
    assert set(by) == {"4917:individ", "7866:hsa"}
    one = by["4917:individ"]
    assert (
        one["broker"] == "fidelity"
        and one["label"] == "Fidelity …4917"
        and one["total_value"] == 1000.0
    )
    assert (
        one["holdings"]["VTI"] == {"quantity": 3.0, "market_value": 600.0}
        and one["as_of"] == "2026-02-28"
    )


def test_statement_accounts_take_a_later_balance_from_the_series_next_to_the_export(tmp_path):
    path = tmp_path / "h.json"
    path.write_text(
        json.dumps(
            {
                "holdings": [
                    {
                        "ticker": "VTI",
                        "accounts": [
                            {
                                "institution": "fid",
                                "account_mask": "***9422",
                                "account_subtype": "hsa",
                                "ending_value": 31500.0,
                                "quantity": 3,
                                "period_end": "2026-02-28",
                            }
                        ],
                    }
                ]
            }
        )
    )
    (tmp_path / "fid").mkdir()
    (tmp_path / "fid" / "timeseries-fidelity-hsa-9422.csv").write_text(
        "account_name,account_mask,period_end,ending_balance\n"
        "HSA,***9422,2026-09-30,18147.89\n"
        "HSA,***9422,2026-10-31,17969.06\n"
    )
    got = a.statement_accounts(path)
    one = got["accounts"][0]
    assert one["total_value"] == 17969.06 and one["as_of"] == "2026-10-31"
    assert one["holdings"]["VTI"]["market_value"] == 31500.0
    assert got["institutions"]["fid"]["total"] == 17969.06


def test_statement_balances_tell_apart_accounts_that_share_digits_and_add_a_recent_one(tmp_path):
    def acct(subtype, value):
        return {
            "institution": "fid",
            "account_mask": "***7866",
            "account_subtype": subtype,
            "ending_value": value,
            "quantity": 1,
            "period_end": "2026-02-28",
        }

    path = tmp_path / "h.json"
    path.write_text(
        json.dumps(
            {
                "holdings": [
                    {"ticker": "VTI", "accounts": [acct("bl-trad", 50000.0), acct("hsa", 9.0)]}
                ]
            }
        )
    )
    (tmp_path / "fid").mkdir()
    (tmp_path / "fid" / "timeseries-fidelity-brokeragelink-traditional-7866.csv").write_text(
        "account_name,account_mask,period_end,ending_balance\n"
        "Fidelity BrokerageLink Traditional,***7866,2026-10-31,70672.0\n"
    )
    (tmp_path / "fid" / "timeseries-fidelity-401-k-401k.csv").write_text(
        "account_name,account_mask,period_end,ending_balance\n"
        "Fidelity 401(k),***401K,2026-02-28,110642.28\n"
        "Fidelity 401(k),***401K,2026-10-29,172439.06\n"
    )
    (tmp_path / "fid" / "timeseries-fidelity-old-1111.csv").write_text(
        "account_name,account_mask,period_end,ending_balance\nFidelity Individual,***1111,2025-01-31,5.0\n"
    )
    got = a.statement_accounts(path)
    by = {x["key"]: x for x in got["accounts"]}
    assert by["7866:bl-trad"]["total_value"] == 70672.0
    assert by["7866:hsa"]["total_value"] == 9.0
    assert by["401K:401k"]["total_value"] == 172439.06 and by["401K:401k"]["holdings"] == {}
    assert not any(k.startswith("1111") for k in by)
    assert got["institutions"]["fid"]["total"] == 70672.0 + 9.0 + 172439.06
