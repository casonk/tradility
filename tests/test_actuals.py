from datetime import date

from tradility.actuals import compute_actuals, position

END = date(2026, 11, 10)
START = date(2026, 10, 11)  # a 30-day window, so a fill on 10/26 has half of it left


def fill(day, side, quantity, price, source="schwab"):
    return {
        "date": day,
        "side": side,
        "quantity": quantity,
        "price": price,
        "source": source,
        "status": "filled",
    }


def test_a_buy_mid_window_is_weighted_by_the_time_it_was_held():
    # 10 shares now at 110; 5 were bought on 10/26 at 105, so 5 were held at the start (price 100)
    gain, base = position(10, 110, 10.0, [fill("2026-10-26", "buy", 5, 105)], START, END)
    assert gain == 1100 - 500 - 525 == 75
    assert base == 500 + 525 * (15 / 30) == 762.5


def test_a_sale_mid_window_takes_cash_out():
    # 5 left at 110 after selling 5 at 108; 10 were held at the start (price 100)
    gain, base = position(5, 110, 10.0, [fill("2026-10-26", "sell", 5, 108)], START, END)
    assert gain == 550 - 1000 + 540 == 90
    assert base == 1000 - 540 * 0.5 == 730


def test_untraded_position_is_just_its_price_return():
    gain, base = position(10, 110, 10.0, [], START, END)
    assert (gain, base) == (100.0, 1000.0)


def test_fills_outside_the_window_are_ignored():
    old = fill("2026-09-01", "buy", 3, 90)
    assert position(10, 110, 10.0, [old], START, END) == (100.0, 1000.0)


def test_more_bought_than_held_is_unreconcilable():  # e.g. a reverse split
    assert position(10, 110, 10.0, [fill("2026-10-26", "buy", 50, 105)], START, END) is None


def test_opened_inside_the_window_needs_no_start_price():
    gain, base = position(5, 110, None, [fill("2026-10-26", "buy", 5, 100)], START, END)
    assert (gain, base) == (50.0, 250.0)


def test_held_at_the_start_without_a_start_price_cannot_be_valued():
    assert position(10, 110, None, [], START, END) is None


def _analysis():
    return {
        "generated_at": "2026-11-10T12:00:00Z",
        "tickers": [
            {
                "ticker": "AAA",
                "asset_class": "Stocks",
                "last_price": 110.0,
                "returns": {"1m": 10.0, "1y": 20.0},
            },
            {
                "ticker": "BTC",
                "asset_class": "Crypto",
                "last_price": 200.0,
                "returns": {"1m": 0.0, "1y": 0.0},
            },
            {"ticker": "OPT", "asset_class": "Options", "last_price": 1.0, "returns": {"1m": 0.0}},
        ],
    }


def _holdings():
    return [
        {
            "ticker": "AAA",
            "asset_class": "Stocks",
            "total_quantity": 10,
            "accounts": [{"institution": "cs"}],
        },
        {
            "ticker": "BTC",
            "asset_class": "Crypto",
            "total_quantity": 2,
            "accounts": [{"institution": "bin"}],
        },
        {
            "ticker": "OPT",
            "asset_class": "Options",
            "total_quantity": 1,
            "accounts": [{"institution": "cs"}],
        },
    ]


def test_compute_actuals_maps_brokers_skips_options_and_limits_windows_to_the_history():
    orders = {
        "orders": {
            "AAA": [fill("2026-10-26", "buy", 5, 105), fill("2025-11-12", "buy", 1, 50)],
            "BTC-USD": [fill("2025-06-26", "buy", 2, 100, source="binance")],
        }
    }
    out = compute_actuals(_analysis(), _holdings(), orders, windows=["1m", "1y"])
    assert out["tickers"]["AAA"]["1m"] == [
        75.0,
        round(500 + 525 * 15 / 31, 2),
    ]  # one calendar month = 31 days
    # the 1y window starts 2025-11-10, three days before Schwab's history, which is inside the slack
    assert "1y" in out["tickers"]["AAA"]
    assert "OPT" not in out["tickers"]
    assert out["history_starts"] == {"schwab": "2025-11-12", "binance": "2025-06-26"}
    assert out["windows"]["1m"]["positions"] == 2 and out["windows"]["1m"]["of"] == 2
    assert 0 < out["windows"]["1m"]["value_share"] <= 1


def test_a_window_older_than_the_order_history_is_not_computed():
    orders = {"orders": {"AAA": [fill("2026-10-26", "buy", 5, 105)]}}
    out = compute_actuals(_analysis(), _holdings(), orders, windows=["1m", "1y"])
    assert out["windows"]["1y"]["available"] is False
    assert "1y" not in out["tickers"].get("AAA", {})


def _income(rows, generated="2026-11-10T12:00:00Z"):
    return {"generated_at": generated, "sources": {"schwab": {"ok": True}}, "income": rows}


def _name_analysis():
    a = _analysis()
    a["tickers"][0]["company_profile"] = {"canonical_name": "Texas Instruments Incorporated"}
    return a


def test_dividends_received_in_the_window_add_to_the_gain_but_not_the_base():
    orders = {
        "orders": {"AAA": [fill("2026-10-26", "buy", 5, 105), fill("2025-11-12", "buy", 1, 50)]}
    }
    paid = [
        {
            "kind": "dividend",
            "description": "TEXAS INSTRS INC",
            "symbol": "",
            "amount": 4.0,
            "date": "2026-10-20",
        },
        {
            "kind": "dividend",
            "description": "TEXAS INSTRS INC",
            "symbol": "",
            "amount": 9.0,
            "date": "2026-08-01",
        },
        {
            "kind": "interest",
            "description": "SCHWAB GOVERNMENT MONEY INV",
            "symbol": "",
            "amount": 2.5,
            "date": "2026-10-20",
        },
        {
            "kind": "dividend",
            "description": "NO SUCH COMPANY",
            "symbol": "",
            "amount": 1.0,
            "date": "2026-10-20",
        },
    ]
    out = compute_actuals(
        _name_analysis(), _holdings(), orders, windows=["1m"], income=_income(paid)
    )
    gain, base, dividends = out["tickers"]["AAA"]["1m"]
    assert dividends == 4.0  # the August payment is outside the month
    assert gain == 75.0 + 4.0 and base == round(500 + 525 * 15 / 31, 2)
    assert out["income"]["1m"] == {"included": 4.0, "left_out": 1.0, "interest": 2.5}


def test_without_an_income_export_entries_are_price_only():
    orders = {
        "orders": {"AAA": [fill("2026-10-26", "buy", 5, 105), fill("2025-11-12", "buy", 1, 50)]}
    }
    out = compute_actuals(_analysis(), _holdings(), orders, windows=["1m"])
    assert len(out["tickers"]["AAA"]["1m"]) == 2 and out["income"] == {}


def test_a_failed_income_export_is_ignored():
    orders = {
        "orders": {"AAA": [fill("2026-10-26", "buy", 5, 105), fill("2025-11-12", "buy", 1, 50)]}
    }
    bad = {
        "generated_at": "2026-11-10T12:00:00Z",
        "sources": {"schwab": {"ok": False}},
        "income": [],
    }
    out = compute_actuals(_analysis(), _holdings(), orders, windows=["1m"], income=bad)
    assert out["income"] == {}


def test_a_price_rounded_to_zero_does_not_become_a_total_loss():
    analysis = _analysis()
    analysis["tickers"][1]["last_price"] = 0.0  # a coin worth less than the price's four decimals
    out = compute_actuals(analysis, _holdings(), {"orders": {}}, windows=["1m"])
    assert "BTC" not in out["tickers"]
