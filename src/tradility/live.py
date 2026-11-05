"""Move a ticker's indicators with a live price, using the bars the analysis stored."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from tradility._compat import UTC
from tradility.analyze import indicator_fields

NEW_YORK = ZoneInfo("America/New_York")


def today_dates() -> set[str]:
    now = datetime.now(UTC)
    return {now.date().isoformat(), now.astimezone(NEW_YORK).date().isoformat()}


def live_indicators(
    record: dict[str, Any],
    price: float,
    day: dict[str, float] | None = None,
    today: set[str] | None = None,
) -> dict[str, Any] | None:
    """Indicator fields as if today's bar closed at ``price``; None when the bars are missing.

    A last stored bar dated today is the market's own in-progress bar, so it is replaced, not
    followed by a second one. ``day`` may carry the session's ``high``, ``low`` and ``volume``;
    without them a new bar has no volume and VWAP keeps its stored values.
    """
    bars = record.get("live_bars")
    if not isinstance(bars, dict) or not bars.get("close") or not price > 0:
        return None
    close = [float(v) for v in bars["close"]]
    high, low, volume = (list(map(float, bars.get(k) or [])) for k in ("high", "low", "volume"))
    day = day or {}
    if bars.get("date") in (today if today is not None else today_dates()):
        close[-1] = price
        if high:
            high[-1] = max(float(day.get("high") or 0), high[-1], price)
            low[-1] = min(float(day.get("low") or price), low[-1], price)
            volume[-1] = max(float(day.get("volume") or 0), volume[-1])
        have_volume = bool(high)
    else:
        close.append(price)
        have_volume = bool(day.get("high") and day.get("low") and day.get("volume") and high)
        if have_volume:
            high.append(max(float(day["high"]), price))
            low.append(min(float(day["low"]), price))
            volume.append(float(day["volume"]))
    series = pd.Series(close, dtype="float64")
    if have_volume:
        n = len(high)
        # the OHLC arrays cover the latest bars only; align them to the end of the close series
        fields = indicator_fields(
            series,
            pd.Series([float("nan")] * (len(series) - n) + high),
            pd.Series([float("nan")] * (len(series) - n) + low),
            pd.Series([float("nan")] * (len(series) - n) + volume),
        )
    else:
        stored = (record.get("indicator_variants") or {}).get("vwap") or {}
        fields = indicator_fields(series, None, None, None, fallback_vwap=stored)
    return {"last_price": round(price, 4), **fields}
