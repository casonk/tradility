"""Orchestration: load tickers → fetch OHLCV → compute indicators → serialize JSON."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tradility.fetch import OHLCVResult, fetch_ohlcv, normalize_history_period
from tradility.indicators import long_term_trend_signal, rsi, rsi_signal, sma, vwap, vwap_signal

RSI_PERIOD = 14
VWAP_PERIOD = 20
SMA_SHORT_PERIOD = 50
SMA_LONG_PERIOD = 200


def _safe_float(value: Any, ndigits: int = 4) -> float | None:
    try:
        v = float(value)
        import math

        return None if math.isnan(v) or math.isinf(v) else round(v, ndigits)
    except (TypeError, ValueError):
        return None


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
    period_end = df.index[-1].date().isoformat()

    rsi_series = rsi(df["Close"], period=RSI_PERIOD)
    rsi_val = _safe_float(rsi_series.iloc[-1], 2)

    vwap_series = vwap(df["High"], df["Low"], df["Close"], df["Volume"], period=VWAP_PERIOD)
    vwap_val = _safe_float(vwap_series.iloc[-1], 4)

    sma_short = _safe_float(sma(df["Close"], SMA_SHORT_PERIOD).iloc[-1], 4)
    sma_long = _safe_float(sma(df["Close"], SMA_LONG_PERIOD).iloc[-1], 4)

    base.update(
        {
            "fetch_status": "ok",
            "last_price": last_price,
            "period_end": period_end,
            f"rsi_{RSI_PERIOD}": rsi_val,
            f"rsi_{RSI_PERIOD}_signal": rsi_signal(rsi_val),
            f"vwap_{VWAP_PERIOD}": vwap_val,
            f"vwap_{VWAP_PERIOD}_signal": vwap_signal(last_price, vwap_val),
            f"sma_{SMA_SHORT_PERIOD}": sma_short,
            f"sma_{SMA_LONG_PERIOD}": sma_long,
            "long_term_trend": long_term_trend_signal(last_price, sma_short, sma_long),
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
