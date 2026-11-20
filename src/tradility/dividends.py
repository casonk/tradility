"""Dividend-adjust Schwab price bars so every ticker's returns are total returns.

Schwab's price history is the raw close (adjusted for splits, not for dividends), while the
yfinance bars and the benchmark ETFs and indexes are dividend-adjusted. Comparing the two
understated a dividend payer's return by roughly its yield each year. This module applies the
same backward adjustment Yahoo uses: for each ex-dividend date, every earlier bar is scaled by
``1 - dividend / close before the ex-date``. The latest bar is never changed, so the last price
stays the real price.

Dividend history comes from Yahoo (Schwab's price history carries none) and is cached on disk for
a day, because it changes only when a company declares one.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

LOGGER = logging.getLogger(__name__)

DIVIDEND_CACHE_SCHEMA = 1
DEFAULT_MAX_AGE_HOURS = 24.0
PRICE_COLUMNS = ("Open", "High", "Low", "Close")

# ticker -> [(ex-date, cash amount per share), ...]
DividendFn = Callable[[str], list[tuple[str, float]]]


def fetch_yfinance_dividends(ticker: str) -> list[tuple[str, float]]:
    """Cash dividends for one symbol from Yahoo, as (ISO ex-date, amount). Raises on failure."""
    import yfinance as yf

    series = yf.Ticker(ticker).dividends
    if series is None or len(series) == 0:
        return []
    index = pd.DatetimeIndex(series.index)
    if index.tz is not None:
        index = index.tz_localize(None)
    return [
        (stamp.strftime("%Y-%m-%d"), float(amount))
        for stamp, amount in zip(index, series.to_numpy(), strict=True)
        if pd.notna(amount) and float(amount) > 0
    ]


def _load_cache(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, dict) or payload.get("schema_version") != DIVIDEND_CACHE_SCHEMA:
        return {}
    return {k: v for k, v in entries.items() if isinstance(v, dict)}


def _save_cache(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"schema_version": DIVIDEND_CACHE_SCHEMA, "entries": entries}, indent=1),
            encoding="utf-8",
        )
    except OSError as exc:
        LOGGER.warning("could not write the dividend cache %s: %s", path, exc)


def load_dividends(
    tickers: list[str],
    *,
    cache_path: Path,
    fetch_fn: DividendFn = fetch_yfinance_dividends,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now: datetime | None = None,
) -> tuple[dict[str, list[tuple[str, float]]], dict[str, str]]:
    """Dividend history per ticker, from the cache or Yahoo; and the errors for tickers with none.

    A failed download reuses a stale cached entry rather than leaving the ticker unadjusted.
    """
    now = now or datetime.now(timezone.utc)
    cache = _load_cache(cache_path)
    found: dict[str, list[tuple[str, float]]] = {}
    errors: dict[str, str] = {}
    changed = False
    for ticker in tickers:
        entry = cache.get(ticker)
        fresh = False
        if entry is not None:
            try:
                fresh = now - datetime.fromisoformat(entry["fetched_at"]) < timedelta(
                    hours=max_age_hours
                )
            except (KeyError, TypeError, ValueError):
                entry = None
        if entry is not None and fresh:
            found[ticker] = [(str(d), float(a)) for d, a in entry["dividends"]]
            continue
        try:
            dividends = fetch_fn(ticker)
        except Exception as exc:  # network, parsing or Yahoo throttling: never fail the run
            LOGGER.warning("dividend history for %s unavailable: %s", ticker, exc)
            if entry is not None:
                found[ticker] = [(str(d), float(a)) for d, a in entry["dividends"]]
            else:
                errors[ticker] = str(exc)
            continue
        found[ticker] = dividends
        cache[ticker] = {
            "fetched_at": now.isoformat(),
            "dividends": [[d, a] for d, a in dividends],
        }
        changed = True
    if changed:
        _save_cache(cache_path, cache)
    return found, errors


def adjust_for_dividends(df: pd.DataFrame, dividends: list[tuple[str, float]]) -> pd.DataFrame:
    """Scale the bars before each ex-dividend date; the latest bar and Volume are unchanged."""
    if df.empty or not dividends:
        return df
    adjusted = df.copy()
    index = pd.DatetimeIndex(adjusted.index)
    factor = pd.Series(1.0, index=adjusted.index)
    for ex_date, amount in sorted(dividends, reverse=True):
        stamp = pd.Timestamp(ex_date)
        before = adjusted["Close"][index < stamp]
        if before.empty or amount <= 0:
            continue
        previous_close = float(before.iloc[-1])
        if previous_close <= 0 or amount >= previous_close:
            continue
        factor[index < stamp] *= 1.0 - amount / previous_close
    for column in PRICE_COLUMNS:
        if column in adjusted:
            adjusted[column] = adjusted[column] * factor
    return adjusted
