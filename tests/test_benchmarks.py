from datetime import UTC, datetime

import pandas as pd

from tradility.benchmarks import (
    MARKET_SYMBOL,
    SECTOR_ETFS,
    SPDR_ETFS,
    load_benchmarks,
    yahoo_key,
)


def _returns(close):
    return {"1m": float(close.iloc[-1] - close.iloc[0]), "1y": None}


def test_yahoo_keys_drop_ampersands_like_yahoo_does():
    assert yahoo_key("Aerospace & Defense") == "aerospace-defense"
    assert yahoo_key("Oil & Gas E&P") == "oil-gas-e-p"
    assert yahoo_key("Drug Manufacturers - General") == "drug-manufacturers-general"
    assert yahoo_key("Financial Services") == "financial-services"


def test_benchmarks_are_fetched_once_cached_and_tolerate_a_bad_index(tmp_path):
    cache = tmp_path / "bench.json"
    calls = []

    def history(symbol):
        calls.append(symbol)
        if symbol == "BAD":
            raise RuntimeError("no data")
        return pd.Series([1.0, 3.0])

    def symbols(kind, key):
        return "BAD" if key == "broken" else f"^{kind[:3]}-{key}"

    now = datetime(2026, 11, 6, tzinfo=UTC)
    kwargs = {
        "cache_path": cache,
        "returns_fn": _returns,
        "now": now,
        "history_fn": history,
        "symbol_fn": symbols,
        "workers": 1,
    }
    first = load_benchmarks(["Technology"], ["Semis", "Broken"], **kwargs)
    assert first["market"]["symbol"] == MARKET_SYMBOL
    assert MARKET_SYMBOL == "VTI"
    assert first["sectors"]["Technology"]["symbol"] == SECTOR_ETFS["Technology"] == "VGT"
    assert first["sectors"]["Technology"]["returns"]["1m"] == 2.0
    sets = first["sector_sets"]
    assert first["default_sector_set"] == "vanguard" and set(sets) == {"vanguard", "spdr", "yahoo"}
    assert sets["vanguard"]["sectors"] == first["sectors"]
    assert sets["spdr"]["sectors"]["Technology"]["symbol"] == SPDR_ETFS["Technology"] == "XLK"
    assert sets["yahoo"]["sectors"]["Technology"]["symbol"] == "^sec-technology"
    assert sets["spdr"]["label"] == "SPDR Select Sector ETFs"
    assert "Semis" in first["industries"] and "Broken" not in first["industries"]
    assert first["summary"] == {
        "source": "yfinance/yahoo-finance",
        "requested": 6,  # the market, Technology in three sector sets, two industries
        "available": 5,
        "errors": 1,
    }

    calls.clear()
    load_benchmarks(["Technology"], ["Semis"], **kwargs)
    assert calls == []  # everything fresh came from the cache

    # a sector Vanguard has no fund for falls back to Yahoo's own sector index
    odd = load_benchmarks(["Made Up"], [], **kwargs)
    for chosen in odd["sector_sets"].values():
        assert chosen["sectors"]["Made Up"]["symbol"] == "^sec-made-up"


def test_a_series_with_an_impossible_daily_move_is_flagged_suspect(tmp_path):
    def history(symbol):
        if symbol == "^sec-glitchy":
            return pd.Series([100.0, 147.0, 103.0, 104.0])  # +47% then -30% in two days
        return pd.Series([100.0, 101.0, 102.0, 103.0])

    out = load_benchmarks(
        ["Glitchy"],
        ["Steady"],
        cache_path=tmp_path / "bench.json",
        returns_fn=lambda close: {"1m": float(close.iloc[-1] / close.iloc[0] - 1) * 100},
        history_fn=history,
        symbol_fn=lambda kind, key: f"^{kind[:3]}-{key}",
        workers=1,
    )
    assert out["sector_sets"]["yahoo"]["sectors"]["Glitchy"]["suspect"] is True
    assert "suspect" not in out["industries"]["Steady"]
    assert "suspect" not in out["market"]
