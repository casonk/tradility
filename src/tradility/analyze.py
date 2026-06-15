"""Orchestration: load tickers → fetch OHLCV → compute indicators → serialize JSON."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tradility.fetch import OHLCVResult, fetch_ohlcv
from tradility.indicators import rsi, rsi_signal, vwap, vwap_signal

RSI_PERIOD = 14
VWAP_PERIOD = 20


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
        "data_source": "yfinance",
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

    base.update(
        {
            "fetch_status": "ok",
            "last_price": last_price,
            "period_end": period_end,
            f"rsi_{RSI_PERIOD}": rsi_val,
            f"rsi_{RSI_PERIOD}_signal": rsi_signal(rsi_val),
            f"vwap_{VWAP_PERIOD}": vwap_val,
            f"vwap_{VWAP_PERIOD}_signal": vwap_signal(last_price, vwap_val),
        }
    )
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
    period_days: int = 90,
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

    ohlcv = fetch_ohlcv(ticker_list, period_days=period_days)

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

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "period_days": period_days,
        "rsi_period": RSI_PERIOD,
        "vwap_period": VWAP_PERIOD,
        "data_source": "yfinance",
        "ticker_count": len(records),
        "fetch_ok": ok_count,
        "fetch_errors": err_count,
        "tickers": records,
    }
