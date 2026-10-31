"""Analysis orchestration tests."""

from __future__ import annotations

import pandas as pd

from tradility import analyze as analyze_module
from tradility.fetch import OHLCVResult


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
    assert payload["tickers"][0]["long_term_trend"] == "bullish"
    assert payload["data_sources"] == {"schwab": 1}
