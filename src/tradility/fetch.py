"""yfinance OHLCV download wrapper."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import yfinance as yf


@dataclass
class OHLCVResult:
    ticker: str
    df: pd.DataFrame = field(default_factory=pd.DataFrame)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and not self.df.empty


def fetch_ohlcv(
    tickers: list[str],
    period_days: int = 90,
    interval: str = "1d",
) -> dict[str, OHLCVResult]:
    """Download OHLCV bars for each ticker via yfinance.

    Returns a dict keyed by uppercase ticker. Each value is an OHLCVResult
    with a DataFrame containing columns: Open, High, Low, Close, Volume.
    """
    results: dict[str, OHLCVResult] = {}
    for ticker in tickers:
        t = ticker.strip().upper()
        try:
            raw: Any = yf.download(
                t,
                period=f"{period_days}d",
                interval=interval,
                auto_adjust=True,
                progress=False,
            )
            if raw.empty:
                results[t] = OHLCVResult(ticker=t, error="no data returned")
                continue
            # Flatten multi-level columns that yfinance may produce
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
            df.index = pd.to_datetime(df.index)
            df = df.sort_index()
            results[t] = OHLCVResult(ticker=t, df=df)
        except Exception as exc:
            results[t] = OHLCVResult(ticker=t, error=str(exc))
    return results
