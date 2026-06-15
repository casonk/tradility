"""Technical indicator calculations: RSI and VWAP."""

from __future__ import annotations

import pandas as pd


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Compute Wilder's RSI over a closing price series.

    Uses exponential smoothing with com=period-1 to match the standard
    Wilder smoothing (equivalent to alpha=1/period).
    Returns a Series of the same index with values in [0, 100].
    """
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    # When avg_loss is 0 and avg_gain > 0 → RSI = 100 (all gains, no losses).
    # Replace 0 avg_loss with NaN only for the RS division; patch the result after.
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    result = 100 - (100 / (1 + rs))
    all_gain_mask = (avg_loss == 0) & avg_gain.notna() & (avg_gain > 0)
    result = result.where(~all_gain_mask, other=100.0)
    all_loss_mask = (avg_gain == 0) & avg_loss.notna() & (avg_loss > 0)
    result = result.where(~all_loss_mask, other=0.0)
    return result


def rsi_signal(value: float | None) -> str:
    """Classify an RSI value into oversold / neutral / overbought."""
    if value is None or pd.isna(value):
        return "unknown"
    if value <= 30:
        return "oversold"
    if value >= 70:
        return "overbought"
    return "neutral"


def vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: int = 20,
) -> pd.Series:
    """Compute rolling VWAP over daily bars.

    True intraday VWAP resets each session; for daily bar data this is a
    rolling-window proxy: sum(typical_price * volume) / sum(volume) over
    the trailing `period` bars.

    typical_price = (High + Low + Close) / 3
    """
    typical_price = (high + low + close) / 3
    tp_vol = typical_price * volume
    return (
        tp_vol.rolling(period, min_periods=period).sum()
        / volume.rolling(period, min_periods=period).sum()
    )


def vwap_signal(last_price: float | None, vwap_value: float | None, tol: float = 0.005) -> str:
    """Classify price relative to VWAP.

    tol: fractional tolerance band around VWAP considered 'at' (default 0.5%).
    """
    if last_price is None or vwap_value is None:
        return "unknown"
    if pd.isna(last_price) or pd.isna(vwap_value):
        return "unknown"
    ratio = (last_price - vwap_value) / vwap_value
    if abs(ratio) <= tol:
        return "at"
    return "above" if ratio > 0 else "below"
