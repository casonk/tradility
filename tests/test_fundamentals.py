from __future__ import annotations

from tradility import fundamentals as fu

STRONG = {
    "peRatio": 12.0,
    "pegRatio": 0.8,
    "returnOnEquity": 22.0,
    "netProfitMarginTTM": 20.0,
    "revChangeYear": 15.0,
    "epsChangePercentTTM": 25.0,
    "totalDebtToEquity": 30.0,
    "currentRatio": 2.0,
    "interestCoverage": 12.0,
    "shortIntToFloat": 1.0,
}
WEAK = {
    "peRatio": -4.0,
    "pegRatio": 3.0,
    "returnOnEquity": 2.0,
    "netProfitMarginTTM": 1.0,
    "revChangeYear": -5.0,
    "epsChangePercentTTM": -30.0,
    "totalDebtToEquity": 300.0,
    "currentRatio": 0.5,
    "interestCoverage": 1.0,
    "shortIntToFloat": 15.0,
}


def test_zero_is_unknown_except_where_it_is_a_real_value():
    got = fu.normalize({"peRatio": 0.0, "currentRatio": 0.0, "dividendYield": 0.0, "beta": 1.1})
    assert got == {"dividend_yield": 0.0, "beta": 1.1}


def test_strong_company_reads_strong_buy():
    read = fu.signals(fu.normalize(STRONG))
    assert {r["signal"] for r in read} == {"buy"}
    assert fu.fundamental_rating(read) == ("strong_buy", len(read), len(read))


def test_weak_company_reads_strong_sell_and_losses_count_against_valuation():
    read = fu.signals(fu.normalize(WEAK))
    by = {r["key"]: r["signal"] for r in read}
    assert by["pe"] == "sell" and by["debt_to_equity"] == "sell"
    assert fu.fundamental_rating(read)[0] == "strong_sell"


def test_too_few_figures_give_no_rating():
    read = fu.signals(fu.normalize({"peRatio": 10.0, "returnOnEquity": 20.0}))
    assert fu.fundamental_rating(read)[0] == "unavailable"


def test_funds_get_figures_but_no_signals():
    fields = fu.record_fields({"asset_type": "ETF", "figures": fu.normalize(STRONG)})
    assert "fundamentals" in fields and "fundamental_rating" not in fields
    equity = fu.record_fields({"asset_type": "EQUITY", "figures": fu.normalize(STRONG)})
    assert equity["fundamental_rating"] == "strong_buy"


def test_fetch_reads_chunks_and_survives_a_failing_one():
    calls = []

    def get(url, headers):
        calls.append(url)
        assert headers["Authorization"] == "Bearer t"
        if len(calls) == 1:
            raise OSError("boom")
        return {
            "instruments": [
                {"symbol": "AAA", "assetType": "EQUITY", "fundamental": {"peRatio": 10.0}},
                {"symbol": "BBB", "assetType": "EQUITY", "fundamental": {"peRatio": 0.0}},
            ]
        }

    symbols = [f"S{i:03d}" for i in range(fu.CHUNK)] + ["AAA", "BBB"]
    got = fu.fetch_schwab_fundamentals(symbols, "t", get)
    assert len(calls) == 2 and set(got) == {"AAA"}
    assert got["AAA"]["figures"] == {"pe": 10.0}


def test_a_zero_short_interest_is_unknown_not_a_neutral_rule():
    assert "short_float" not in fu.normalize({"shortIntToFloat": 0.0, "beta": 1.0})
    keys = {r["key"] for r in fu.signals(fu.normalize({**STRONG, "shortIntToFloat": 0.0}))}
    assert "short_float" not in keys
