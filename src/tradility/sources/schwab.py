"""Charles Schwab Trader API price-history adapter.

OAuth ownership deliberately remains outside Tradility. An access token can be
provided directly or by a configured command that reuses the portfolio's
governed credential pipeline. Token values are never logged or serialized.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd


@dataclass
class SchwabOHLCVResult:
    ticker: str
    df: pd.DataFrame = field(default_factory=pd.DataFrame)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and not self.df.empty


@dataclass
class AccessTokenResolution:
    token: str = ""
    source: str = ""
    error: str = ""


def resolve_access_token(
    explicit_token: str = "",
    token_command: str = "",
    timeout_seconds: int = 30,
) -> AccessTokenResolution:
    """Resolve an access token without exposing it in diagnostics."""
    token = explicit_token.strip()
    if token:
        return AccessTokenResolution(token=token, source="environment")
    command = token_command.strip()
    if not command:
        return AccessTokenResolution(error="Schwab access token is not configured")
    try:
        completed = subprocess.run(
            shlex.split(command),
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return AccessTokenResolution(error=f"Schwab token command failed: {type(exc).__name__}")
    output = completed.stdout.strip()
    if not output:
        return AccessTokenResolution(error="Schwab token command returned no token")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        token = output
    else:
        token = str(payload.get("access_token", "")).strip() if isinstance(payload, dict) else ""
    if not token or any(char.isspace() for char in token):
        return AccessTokenResolution(error="Schwab token command returned an invalid token")
    return AccessTokenResolution(token=token, source="command")


def _period_parameters(history_period: str) -> dict[str, str | int | bool]:
    if history_period == "max":
        years = 20
    elif history_period.endswith("y"):
        years = int(history_period[:-1])
    else:
        days = int(history_period[:-1])
        years = max(1, (days + 364) // 365)
    allowed_years = (1, 2, 3, 5, 10, 15, 20)
    period = next((candidate for candidate in allowed_years if candidate >= years), 20)
    return {
        "periodType": "year",
        "period": period,
        "frequencyType": "daily",
        "frequency": 1,
        "needExtendedHoursData": "false",
        "needPreviousClose": "true",
    }


def _request_json(
    url: str,
    access_token: str,
    timeout_seconds: int,
    max_retries: int,
) -> Any:
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "tradility/0.2",
        },
    )
    for attempt in range(max_retries + 1):
        try:
            with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt >= max_retries:
                raise
        except URLError:
            if attempt >= max_retries:
                raise
        time.sleep(min(8.0, 0.5 * (2**attempt)))
    raise RuntimeError("Schwab request retries exhausted")


def _candles_to_frame(payload: Any) -> pd.DataFrame:
    candles = payload.get("candles", []) if isinstance(payload, dict) else []
    rows = [row for row in candles if isinstance(row, dict) and row.get("datetime") is not None]
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    required = {"open", "high", "low", "close", "volume", "datetime"}
    if not required.issubset(frame.columns):
        return pd.DataFrame()
    frame.index = pd.to_datetime(frame.pop("datetime"), unit="ms", utc=True).dt.tz_localize(None)
    frame = frame.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )
    return frame[["Open", "High", "Low", "Close", "Volume"]].sort_index()


def fetch_schwab_ohlcv(
    tickers: list[str],
    access_token: str,
    history_period: str = "5y",
    interval: str = "1d",
    base_url: str = "https://api.schwabapi.com",
    timeout_seconds: int = 45,
    max_retries: int = 3,
    throttle_seconds: float = 0.5,
) -> dict[str, SchwabOHLCVResult]:
    """Fetch daily price-history candles for each ticker."""
    if interval != "1d":
        raise ValueError("Schwab source currently supports only the 1d interval")
    results: dict[str, SchwabOHLCVResult] = {}
    for index, raw_ticker in enumerate(tickers):
        ticker = raw_ticker.strip().upper()
        params = {"symbol": ticker, **_period_parameters(history_period)}
        url = f"{base_url.rstrip('/')}/marketdata/v1/pricehistory?{urlencode(params)}"
        try:
            frame = _candles_to_frame(
                _request_json(
                    url,
                    access_token=access_token,
                    timeout_seconds=timeout_seconds,
                    max_retries=max_retries,
                )
            )
            if frame.empty:
                results[ticker] = SchwabOHLCVResult(ticker=ticker, error="no data returned")
            else:
                results[ticker] = SchwabOHLCVResult(ticker=ticker, df=frame)
        except (HTTPError, URLError, ValueError, OSError) as exc:
            status = f" HTTP {exc.code}" if isinstance(exc, HTTPError) else ""
            results[ticker] = SchwabOHLCVResult(
                ticker=ticker,
                error=f"Schwab request failed{status}",
            )
        if throttle_seconds > 0 and index < len(tickers) - 1:
            time.sleep(throttle_seconds)
    return results
