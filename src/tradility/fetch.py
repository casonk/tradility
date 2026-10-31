"""Market-data selection and yfinance fallback."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import yfinance as yf

from tradility.sources.schwab import fetch_schwab_ohlcv, resolve_access_token


@dataclass
class OHLCVResult:
    ticker: str
    df: pd.DataFrame = field(default_factory=pd.DataFrame)
    error: str = ""
    source: str = ""
    fallback_reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and not self.df.empty


def fetch_ohlcv(
    tickers: list[str],
    period_days: int | None = None,
    history_period: str = "5y",
    interval: str = "1d",
    source: str = "auto",
    schwab_tickers: set[str] | None = None,
    schwab_access_token: str = "",
    schwab_token_command: str = "",
) -> dict[str, OHLCVResult]:
    """Download OHLCV bars, preferring Schwab when configured.

    Returns a dict keyed by uppercase ticker. Each value is an OHLCVResult
    with a DataFrame containing columns: Open, High, Low, Close, Volume.

    ``source=auto`` uses Schwab for eligible equity tickers when an access
    token is available, then falls back ticker-by-ticker to yfinance. Crypto
    and other explicitly ineligible tickers always use yfinance.
    """
    normalized_source = source.strip().lower()
    if normalized_source not in {"auto", "schwab", "yfinance"}:
        raise ValueError("source must be auto, schwab, or yfinance")
    if period_days is not None:
        history_period = f"{period_days}d"
    history_period = normalize_history_period(history_period)
    normalized_tickers = [ticker.strip().upper() for ticker in tickers if ticker.strip()]
    eligible = (
        {ticker.strip().upper() for ticker in schwab_tickers}
        if schwab_tickers is not None
        else set(normalized_tickers)
    )

    results: dict[str, OHLCVResult] = {}
    schwab_failure = ""
    if normalized_source in {"auto", "schwab"} and eligible:
        token_resolution = resolve_access_token(
            explicit_token=schwab_access_token
            or os.environ.get("TRADILITY_SCHWAB_ACCESS_TOKEN", ""),
            token_command=schwab_token_command
            or os.environ.get("TRADILITY_SCHWAB_TOKEN_COMMAND", ""),
        )
        if token_resolution.token:
            schwab_results = fetch_schwab_ohlcv(
                sorted(eligible),
                access_token=token_resolution.token,
                history_period=history_period,
                interval=interval,
            )
            for ticker, schwab_result in schwab_results.items():
                results[ticker] = OHLCVResult(
                    ticker=ticker,
                    df=schwab_result.df,
                    error=schwab_result.error,
                    source="schwab",
                )
        else:
            schwab_failure = token_resolution.error or "Schwab access token unavailable"

    if normalized_source == "schwab":
        for ticker in normalized_tickers:
            if ticker not in eligible:
                results[ticker] = OHLCVResult(
                    ticker=ticker,
                    error="ticker is not eligible for Schwab market data",
                    source="schwab",
                )
            elif ticker not in results:
                results[ticker] = OHLCVResult(
                    ticker=ticker,
                    error=schwab_failure,
                    source="schwab",
                )
        return results

    yahoo_tickers = [
        ticker for ticker in normalized_tickers if ticker not in results or not results[ticker].ok
    ]
    yahoo_results = _fetch_yfinance_ohlcv(
        yahoo_tickers,
        history_period=history_period,
        interval=interval,
    )
    for ticker, yahoo_result in yahoo_results.items():
        prior = results.get(ticker)
        if prior is not None and prior.error:
            yahoo_result.fallback_reason = f"Schwab fetch failed: {prior.error}"
        elif schwab_failure and ticker in eligible:
            yahoo_result.fallback_reason = schwab_failure
        results[ticker] = yahoo_result
    return results


def normalize_history_period(value: str) -> str:
    """Normalize CLI/API lookback values while retaining legacy day counts."""
    period = str(value or "").strip().lower()
    if period.isdigit():
        period = f"{period}d"
    if period == "max":
        return period
    if len(period) < 2 or period[-1] not in {"d", "y"} or not period[:-1].isdigit():
        raise ValueError("history period must be DAYS, Nd, Ny, or max (for example 365, 5y)")
    if int(period[:-1]) <= 0:
        raise ValueError("history period must be positive")
    return period


def _fetch_yfinance_ohlcv(
    tickers: list[str],
    history_period: str,
    interval: str,
) -> dict[str, OHLCVResult]:
    """Download OHLCV bars for each ticker via yfinance."""
    results: dict[str, OHLCVResult] = {}
    for ticker in tickers:
        t = ticker.strip().upper()
        try:
            raw: Any = yf.download(
                t,
                period=history_period,
                interval=interval,
                auto_adjust=True,
                progress=False,
            )
            if raw.empty:
                results[t] = OHLCVResult(ticker=t, error="no data returned", source="yfinance")
                continue
            # Flatten multi-level columns that yfinance may produce
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
            df.index = pd.to_datetime(df.index)
            df = df.sort_index()
            results[t] = OHLCVResult(ticker=t, df=df, source="yfinance")
        except Exception as exc:
            results[t] = OHLCVResult(ticker=t, error=str(exc), source="yfinance")
    return results
