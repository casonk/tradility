"""Orchestration: load tickers → fetch OHLCV → compute indicators → serialize JSON."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from tradility.fetch import OHLCVResult, fetch_ohlcv, normalize_history_period
from tradility.indicators import long_term_trend_signal, rsi, rsi_signal, sma, vwap, vwap_signal

RSI_PERIOD = 14
VWAP_PERIOD = 20
SMA_SHORT_PERIOD = 50
SMA_LONG_PERIOD = 200
RETURN_WINDOWS = {
    "1m": pd.DateOffset(months=1),
    "3m": pd.DateOffset(months=3),
    "1y": pd.DateOffset(years=1),
    "3y": pd.DateOffset(years=3),
    "5y": pd.DateOffset(years=5),
}


def _safe_float(value: Any, ndigits: int = 4) -> float | None:
    try:
        v = float(value)
        import math

        return None if math.isnan(v) or math.isinf(v) else round(v, ndigits)
    except (TypeError, ValueError):
        return None


def _period_return(close: pd.Series, offset: pd.DateOffset) -> float | None:
    """Return percentage performance from the last close at or before a calendar offset."""
    clean = close.dropna()
    if clean.empty:
        return None
    target = pd.Timestamp(clean.index[-1]) - offset
    prior = clean.loc[clean.index <= target]
    if prior.empty:
        first_date = pd.Timestamp(clean.index[0])
        if first_date - target > pd.Timedelta(days=7):
            return None
        start_value = clean.iloc[0]
    else:
        start_value = prior.iloc[-1]
    start = _safe_float(start_value, 8)
    end = _safe_float(clean.iloc[-1], 8)
    if start in {None, 0} or end is None:
        return None
    return _safe_float((end - start) / start * 100, 2)


def _chart_history(close: pd.Series) -> list[dict[str, str | float]]:
    """Build a chart-ready series with daily recent data and weekly older data."""
    clean = close.dropna().sort_index()
    if clean.empty:
        return []
    cutoff = pd.Timestamp(clean.index[-1]) - pd.DateOffset(years=1)
    older = clean.loc[clean.index < cutoff]
    recent = clean.loc[clean.index >= cutoff]
    if not older.empty:
        older = older.resample("W-FRI").last().dropna()
    chart = pd.concat([older, recent])
    chart = chart.loc[~chart.index.duplicated(keep="last")]
    return [
        {"date": pd.Timestamp(index).date().isoformat(), "close": float(round(value, 4))}
        for index, value in chart.items()
    ]


def _technical_rating(
    rsi_value_signal: str,
    vwap_value_signal: str,
    trend_signal: str,
) -> tuple[str, int, dict[str, int]]:
    """Combine transparent trend, momentum, and mean-reversion components."""
    components = {
        "long_term_trend": {"bullish": 2, "bearish": -2}.get(trend_signal, 0),
        "vwap_momentum": {"above": 1, "below": -1}.get(vwap_value_signal, 0),
        "rsi_reversion": {"oversold": 1, "overbought": -1}.get(rsi_value_signal, 0),
    }
    score = sum(components.values())
    if score >= 3:
        rating = "strong_buy"
    elif score >= 1:
        rating = "buy"
    elif score <= -3:
        rating = "strong_sell"
    elif score <= -1:
        rating = "sell"
    else:
        rating = "hold"
    return rating, score, components


def _ticker_record(ticker: str, result: OHLCVResult, meta: dict[str, str]) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ticker": ticker,
        "name": meta.get("name", ""),
        "asset_class": meta.get("asset_class", ""),
        "data_source": result.source or "unknown",
    }

    if not result.ok:
        base["fetch_status"] = "error"
        base["fetch_error"] = result.error
        return base

    df = result.df
    last_price = _safe_float(df["Close"].iloc[-1], 4)
    period_start = df.index[0].date().isoformat()
    period_end = df.index[-1].date().isoformat()

    rsi_series = rsi(df["Close"], period=RSI_PERIOD)
    rsi_val = _safe_float(rsi_series.iloc[-1], 2)

    vwap_series = vwap(df["High"], df["Low"], df["Close"], df["Volume"], period=VWAP_PERIOD)
    vwap_val = _safe_float(vwap_series.iloc[-1], 4)

    sma_short = _safe_float(sma(df["Close"], SMA_SHORT_PERIOD).iloc[-1], 4)
    sma_long = _safe_float(sma(df["Close"], SMA_LONG_PERIOD).iloc[-1], 4)
    rsi_value_signal = rsi_signal(rsi_val)
    vwap_value_signal = vwap_signal(last_price, vwap_val)
    trend_signal = long_term_trend_signal(last_price, sma_short, sma_long)
    technical_rating, technical_score, technical_components = _technical_rating(
        rsi_value_signal,
        vwap_value_signal,
        trend_signal,
    )

    range_start = pd.Timestamp(df.index[-1]) - pd.DateOffset(years=1)
    range_frame = df.loc[df.index >= range_start]
    high_52w = _safe_float(range_frame["High"].max(), 4)
    low_52w = _safe_float(range_frame["Low"].min(), 4)
    range_position = None
    if (
        last_price is not None
        and high_52w is not None
        and low_52w is not None
        and high_52w != low_52w
    ):
        range_position = _safe_float((last_price - low_52w) / (high_52w - low_52w) * 100, 2)

    base.update(
        {
            "fetch_status": "ok",
            "last_price": last_price,
            "period_start": period_start,
            "period_end": period_end,
            f"rsi_{RSI_PERIOD}": rsi_val,
            f"rsi_{RSI_PERIOD}_signal": rsi_value_signal,
            f"vwap_{VWAP_PERIOD}": vwap_val,
            f"vwap_{VWAP_PERIOD}_signal": vwap_value_signal,
            f"sma_{SMA_SHORT_PERIOD}": sma_short,
            f"sma_{SMA_LONG_PERIOD}": sma_long,
            "long_term_trend": trend_signal,
            "returns": {
                label: _period_return(df["Close"], offset)
                for label, offset in RETURN_WINDOWS.items()
            },
            "high_52w": high_52w,
            "low_52w": low_52w,
            "range_position_52w": range_position,
            "technical_rating": technical_rating,
            "technical_score": technical_score,
            "technical_components": technical_components,
            "price_history": _chart_history(df["Close"]),
        }
    )
    if result.fallback_reason:
        base["fallback_reason"] = result.fallback_reason
    return base


def load_tickers_from_holdings(holdings_path: Path) -> dict[str, dict[str, str]]:
    """Read tickers from a holdings-aggregate.json file.

    Returns a dict of ticker → {name, asset_class}.
    """
    data = json.loads(holdings_path.read_text(encoding="utf-8"))
    result: dict[str, dict[str, str]] = {}
    for h in data.get("holdings", []):
        ticker = str(h.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        # Map Binance crypto tickers to Yahoo Finance format (e.g. BTC → BTC-USD)
        asset_class = str(h.get("asset_class", ""))
        yf_ticker = _to_yf_ticker(ticker, asset_class)
        result[yf_ticker] = {
            "name": str(h.get("description", "") or ""),
            "asset_class": asset_class,
            "original_ticker": ticker,
        }
    return result


def _to_yf_ticker(ticker: str, asset_class: str) -> str:
    """Convert an internal ticker to its yfinance symbol.

    Crypto tickers from Binance (asset_class='Crypto') need a '-USD' suffix
    unless they are already in Yahoo Finance format.
    """
    if asset_class == "Crypto" and "-" not in ticker:
        return f"{ticker}-USD"
    return ticker


def analyze_tickers(
    tickers: list[str] | None = None,
    holdings_path: Path | None = None,
    period_days: int | None = None,
    history_period: str = "5y",
    source: str = "auto",
    schwab_access_token: str = "",
    schwab_token_command: str = "",
) -> dict[str, Any]:
    """Run full analysis pipeline.

    Accepts either an explicit ticker list or a path to holdings-aggregate.json.
    Returns the structured payload suitable for JSON serialization.
    """
    if holdings_path is not None:
        meta_by_ticker = load_tickers_from_holdings(holdings_path)
        ticker_list = list(meta_by_ticker.keys())
    elif tickers:
        ticker_list = [t.strip().upper() for t in tickers]
        meta_by_ticker = {t: {"name": "", "asset_class": ""} for t in ticker_list}
    else:
        raise ValueError("Provide either tickers or holdings_path.")

    resolved_period = f"{period_days}d" if period_days is not None else history_period
    resolved_period = normalize_history_period(resolved_period)
    schwab_tickers = {
        ticker
        for ticker, meta in meta_by_ticker.items()
        if str(meta.get("asset_class", "")).strip().lower() != "crypto"
    }
    ohlcv = fetch_ohlcv(
        ticker_list,
        history_period=resolved_period,
        source=source,
        schwab_tickers=schwab_tickers,
        schwab_access_token=schwab_access_token,
        schwab_token_command=schwab_token_command,
    )

    records = []
    for yf_ticker, meta in meta_by_ticker.items():
        result = ohlcv.get(yf_ticker, OHLCVResult(ticker=yf_ticker, error="not fetched"))
        rec = _ticker_record(yf_ticker, result, meta)
        # Restore original ticker label for crypto (strip -USD suffix in output)
        rec["ticker"] = meta.get("original_ticker", yf_ticker) or yf_ticker
        records.append(rec)

    records.sort(key=lambda r: r["ticker"])

    ok_count = sum(1 for r in records if r.get("fetch_status") == "ok")
    err_count = len(records) - ok_count
    source_counts: dict[str, int] = {}
    for record in records:
        data_source = str(record.get("data_source", "unknown"))
        source_counts[data_source] = source_counts.get(data_source, 0) + 1

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "history_period": resolved_period,
        "period_days": int(resolved_period[:-1]) if resolved_period.endswith("d") else None,
        "rsi_period": RSI_PERIOD,
        "vwap_period": VWAP_PERIOD,
        "sma_periods": [SMA_SHORT_PERIOD, SMA_LONG_PERIOD],
        "requested_source": source,
        "data_source": next(iter(source_counts)) if len(source_counts) == 1 else "mixed",
        "data_sources": source_counts,
        "ticker_count": len(records),
        "fetch_ok": ok_count,
        "fetch_errors": err_count,
        "tickers": records,
    }
