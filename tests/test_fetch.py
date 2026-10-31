"""Market-source selection tests."""

from __future__ import annotations

import pandas as pd
import pytest

from tradility import fetch as fetch_module
from tradility.fetch import OHLCVResult, fetch_ohlcv, normalize_history_period
from tradility.sources.schwab import AccessTokenResolution, SchwabOHLCVResult


def _frame(price: float = 100.0) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=220, freq="B")
    return pd.DataFrame(
        {
            "Open": price,
            "High": price + 1,
            "Low": price - 1,
            "Close": price,
            "Volume": 1000,
        },
        index=index,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("90", "90d"), ("365d", "365d"), ("5Y", "5y"), ("max", "max")],
)
def test_normalize_history_period(raw, expected):
    assert normalize_history_period(raw) == expected


def test_auto_prefers_schwab_and_uses_yahoo_for_crypto(monkeypatch):
    monkeypatch.setattr(
        fetch_module,
        "resolve_access_token",
        lambda **_kwargs: AccessTokenResolution(token="secret", source="test"),
    )
    monkeypatch.setattr(
        fetch_module,
        "fetch_schwab_ohlcv",
        lambda tickers, **_kwargs: {
            ticker: SchwabOHLCVResult(ticker=ticker, df=_frame(110.0)) for ticker in tickers
        },
    )
    monkeypatch.setattr(
        fetch_module,
        "_fetch_yfinance_ohlcv",
        lambda tickers, **_kwargs: {
            ticker: OHLCVResult(ticker=ticker, df=_frame(90.0), source="yfinance")
            for ticker in tickers
        },
    )

    results = fetch_ohlcv(
        ["AAPL", "BTC-USD"],
        history_period="5y",
        source="auto",
        schwab_tickers={"AAPL"},
    )

    assert results["AAPL"].source == "schwab"
    assert results["BTC-USD"].source == "yfinance"


def test_auto_falls_back_without_a_token(monkeypatch):
    monkeypatch.setattr(
        fetch_module,
        "resolve_access_token",
        lambda **_kwargs: AccessTokenResolution(error="token unavailable"),
    )
    monkeypatch.setattr(
        fetch_module,
        "_fetch_yfinance_ohlcv",
        lambda tickers, **_kwargs: {
            ticker: OHLCVResult(ticker=ticker, df=_frame(), source="yfinance") for ticker in tickers
        },
    )

    results = fetch_ohlcv(["AAPL"], source="auto", schwab_tickers={"AAPL"})

    assert results["AAPL"].source == "yfinance"
    assert results["AAPL"].fallback_reason == "token unavailable"
