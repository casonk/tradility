"""Schwab bars are dividend-adjusted so returns are total returns, like yfinance and the benchmarks."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from tradility import fetch as fetch_module
from tradility.dividends import adjust_for_dividends, load_dividends
from tradility.fetch import fetch_ohlcv
from tradility.sources.schwab import AccessTokenResolution, SchwabOHLCVResult


def bars(closes, start="2026-01-01"):
    index = pd.date_range(start, periods=len(closes), freq="D")
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c + 1 for c in closes],
            "Low": [c - 1 for c in closes],
            "Close": closes,
            "Volume": [1000] * len(closes),
        },
        index=index,
    )


def test_bars_before_the_ex_date_are_scaled_and_the_latest_bar_is_not():
    df = bars([100.0, 100.0, 100.0, 98.0, 98.0])  # ex-date 2026-01-04, close before it is 100
    out = adjust_for_dividends(df, [("2026-01-04", 2.0)])
    assert list(out["Close"].round(4)) == [98.0, 98.0, 98.0, 98.0, 98.0]
    assert out["High"].iloc[0] == pytest.approx(101.0 * 0.98)
    assert list(out["Volume"]) == [1000] * 5
    assert out["Close"].iloc[-1] == df["Close"].iloc[-1]
    assert df["Close"].iloc[0] == 100.0  # the input is not modified


def test_two_dividends_compound_backwards():
    df = bars([100.0, 100.0, 100.0, 100.0])
    out = adjust_for_dividends(df, [("2026-01-02", 1.0), ("2026-01-04", 5.0)])
    # day 1: both apply; day 2-3: only the later one; day 4 (the last bar): neither
    assert list(out["Close"].round(4)) == [94.05, 95.0, 95.0, 100.0]


def test_unusable_dividends_are_ignored():
    df = bars([100.0, 100.0, 100.0])
    unchanged = adjust_for_dividends(
        df,
        [
            ("2025-12-01", 1.0),  # before the first bar: nothing to adjust
            ("2026-01-02", 500.0),  # larger than the close: bad data
            ("2026-01-03", -1.0),
        ],
    )
    assert unchanged["Close"].tolist() == [100.0, 100.0, 100.0]
    assert adjust_for_dividends(df, []).equals(df)
    assert adjust_for_dividends(df.iloc[0:0], [("2026-01-02", 1.0)]).empty


NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def test_dividend_history_is_cached_for_a_day(tmp_path):
    cache = tmp_path / "dividends.json"
    calls = []

    def fetch(ticker):
        calls.append(ticker)
        return [("2026-03-01", 0.5)]

    found, errors = load_dividends(["VTI"], cache_path=cache, fetch_fn=fetch, now=NOW)
    assert found == {"VTI": [("2026-03-01", 0.5)]} and errors == {}
    load_dividends(["VTI"], cache_path=cache, fetch_fn=fetch, now=NOW + timedelta(hours=23))
    assert calls == ["VTI"]
    load_dividends(["VTI"], cache_path=cache, fetch_fn=fetch, now=NOW + timedelta(hours=25))
    assert calls == ["VTI", "VTI"]
    assert json.loads(cache.read_text())["schema_version"] == 1


def test_a_failed_download_reuses_the_stale_cache_or_reports_an_error(tmp_path):
    cache = tmp_path / "dividends.json"
    load_dividends(["VTI"], cache_path=cache, fetch_fn=lambda t: [("2026-03-01", 0.5)], now=NOW)

    def down(ticker):
        raise OSError("yahoo is throttling")

    later = NOW + timedelta(days=3)
    found, errors = load_dividends(["VTI", "NEW"], cache_path=cache, fetch_fn=down, now=later)
    assert found == {"VTI": [("2026-03-01", 0.5)]}
    assert "throttling" in errors["NEW"] and "VTI" not in errors


def test_a_corrupt_cache_is_ignored(tmp_path):
    cache = tmp_path / "dividends.json"
    cache.write_text("{not json")
    found, _ = load_dividends(["A"], cache_path=cache, fetch_fn=lambda t: [], now=NOW)
    assert found == {"A": []}


@pytest.fixture
def schwab_only(monkeypatch):
    monkeypatch.setattr(
        fetch_module,
        "resolve_access_token",
        lambda **_kw: AccessTokenResolution(token="t", source="test"),
    )
    monkeypatch.setattr(
        fetch_module,
        "fetch_schwab_ohlcv",
        lambda tickers, **_kw: {
            t: SchwabOHLCVResult(ticker=t, df=bars([100.0, 100.0, 98.0])) for t in tickers
        },
    )


def test_fetch_adjusts_schwab_bars_when_given_a_cache(schwab_only, tmp_path):
    results = fetch_ohlcv(
        ["VTI", "FLAT"],
        source="schwab",
        dividend_cache_path=tmp_path / "d.json",
        dividends_fn=lambda t: [("2026-01-03", 2.0)] if t == "VTI" else [],
    )
    assert results["VTI"].dividend_adjusted and results["VTI"].df["Close"].iloc[0] == 98.0
    assert results["VTI"].df["Close"].iloc[-1] == 98.0
    assert results["FLAT"].dividend_adjusted  # no dividends is a true answer
    assert results["FLAT"].df["Close"].iloc[0] == 100.0


def test_fetch_leaves_bars_raw_and_says_so_when_dividends_are_unavailable(schwab_only, tmp_path):
    def down(ticker):
        raise OSError("no route")

    results = fetch_ohlcv(
        ["VTI"], source="schwab", dividend_cache_path=tmp_path / "d.json", dividends_fn=down
    )
    assert results["VTI"].ok and not results["VTI"].dividend_adjusted
    assert "no route" in results["VTI"].adjustment_note
    assert results["VTI"].df["Close"].iloc[0] == 100.0


def test_without_a_cache_path_nothing_is_adjusted_or_downloaded(schwab_only):
    def boom(ticker):
        raise AssertionError("must not be called")

    results = fetch_ohlcv(["VTI"], source="schwab", dividends_fn=boom)
    assert not results["VTI"].dividend_adjusted
    assert results["VTI"].df["Close"].iloc[0] == 100.0


def test_yfinance_bars_are_marked_adjusted(monkeypatch):
    monkeypatch.setattr(
        fetch_module.yf, "download", lambda *a, **k: bars([10.0, 11.0]).drop(columns=[])
    )
    result = fetch_ohlcv(["X"], source="yfinance")["X"]
    assert result.ok and result.dividend_adjusted
