"""Analysis orchestration tests."""

from __future__ import annotations

import pandas as pd
import pytest

from tradility import analyze as analyze_module
from tradility.fetch import OHLCVResult
from tradility.profiles import ProfileLoadResult


def _trend_frame() -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=260, freq="B")
    close = pd.Series(range(100, 360), index=index, dtype=float)
    return pd.DataFrame(
        {
            "Open": close - 0.5,
            "High": close + 1,
            "Low": close - 1,
            "Close": close,
            "Volume": 1000,
        }
    )


def test_analysis_defaults_to_five_years_and_emits_long_term_trend(monkeypatch):
    observed = {}

    def fake_fetch(tickers, **kwargs):
        observed.update(kwargs)
        return {
            ticker: OHLCVResult(ticker=ticker, df=_trend_frame(), source="schwab")
            for ticker in tickers
        }

    monkeypatch.setattr(analyze_module, "fetch_ohlcv", fake_fetch)

    payload = analyze_module.analyze_tickers(tickers=["AAPL"])

    assert observed["history_period"] == "5y"
    assert payload["history_period"] == "5y"
    assert payload["sma_periods"] == [50, 200]
    record = payload["tickers"][0]
    assert record["long_term_trend"] == "bullish"
    assert record["technical_rating"] == "buy"
    assert record["technical_score"] == 2
    assert record["returns"]["1m"] > 0
    assert record["high_52w"] == 360.0
    assert record["low_52w"] == 99.0
    assert record["period_start"] == "2024-01-01"
    assert record["price_history"][0] == {"date": "2024-01-01", "close": 100.0}
    assert record["price_history"][-1] == {"date": "2024-12-27", "close": 359.0}
    assert payload["data_sources"] == {"schwab": 1}


def test_chart_history_keeps_recent_daily_points_and_downsamples_older_history():
    index = pd.date_range("2021-01-01", periods=1300, freq="B")
    close = pd.Series(range(100, 1400), index=index, dtype=float)

    history = analyze_module._chart_history(close)

    assert history[0]["date"] >= "2021-01-01"
    assert history[-1] == {"date": index[-1].date().isoformat(), "close": 1399.0}
    assert 400 < len(history) < 550
    recent = [point for point in history if point["date"] >= "2025-01-01"]
    assert len(recent) > 200


def test_analysis_attaches_cached_company_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(
        analyze_module,
        "fetch_ohlcv",
        lambda tickers, **_kwargs: {
            ticker: OHLCVResult(ticker=ticker, df=_trend_frame(), source="schwab")
            for ticker in tickers
        },
    )
    observed = {}

    def fake_profiles(tickers, **kwargs):
        observed["tickers"] = list(tickers)
        observed.update(kwargs)
        return ProfileLoadResult(
            profiles={
                "AAPL": {
                    "available": True,
                    "canonical_name": "Apple Inc.",
                    "description": "Makes consumer technology.",
                    "source": "yfinance/yahoo-finance",
                    "retrieved_at": "2026-11-01T00:00:00Z",
                }
            },
            requested=1,
            cache_hits=1,
            refreshed=0,
            errors=0,
        )

    monkeypatch.setattr(analyze_module, "load_company_profiles", fake_profiles)
    cache = tmp_path / "profiles.json"

    payload = analyze_module.analyze_tickers(
        tickers=["AAPL"], include_profiles=True, profile_cache_path=cache
    )

    assert observed["tickers"] == ["AAPL"]
    assert observed["cache_path"] == cache
    assert payload["company_profiles"]["cache_hits"] == 1
    assert payload["tickers"][0]["name"] == "Apple Inc."
    assert payload["tickers"][0]["company_profile"]["description"].startswith("Makes")


def test_profiles_require_an_explicit_cache_path(monkeypatch):
    monkeypatch.setattr(analyze_module, "fetch_ohlcv", lambda *_args, **_kwargs: {})

    with pytest.raises(ValueError, match="profile_cache_path"):
        analyze_module.analyze_tickers(tickers=["AAPL"], include_profiles=True)


def test_period_return_accepts_the_first_market_day_after_a_weekend_boundary():
    close = pd.Series(
        [100.0, 200.0],
        index=pd.to_datetime(["2021-11-01", "2026-10-30"]),
    )

    assert analyze_module._period_return(close, pd.DateOffset(years=5)) == 100.0

    too_short = close.loc[close.index >= "2022-01-01"]
    assert analyze_module._period_return(too_short, pd.DateOffset(years=5)) is None


@pytest.mark.parametrize(
    ("rsi_signal", "vwap_signal", "trend", "rating", "score"),
    [
        ("oversold", "above", "bullish", "strong_buy", 4),
        ("neutral", "at", "mixed", "hold", 0),
        ("overbought", "below", "bearish", "strong_sell", -4),
    ],
)
def test_technical_rating_is_a_transparent_component_sum(
    rsi_signal, vwap_signal, trend, rating, score
):
    actual_rating, actual_score, components = analyze_module._technical_rating(
        rsi_signal,
        vwap_signal,
        trend,
    )

    assert actual_rating == rating
    assert actual_score == score
    assert sum(components.values()) == score
