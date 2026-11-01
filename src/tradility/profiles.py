"""Normalize and cache public company/fund profiles from market-data sources."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from tradility.fetch import fetch_yfinance_profile

PROFILE_SOURCE = "yfinance/yahoo-finance"
PROFILE_CACHE_SCHEMA = 1


@dataclass(frozen=True)
class ProfileLoadResult:
    """Profiles plus cache/refresh counts suitable for artifact metadata."""

    profiles: dict[str, dict[str, Any]]
    requested: int
    cache_hits: int
    refreshed: int
    errors: int


def _text(value: Any, *, limit: int = 8_000) -> str:
    return str(value or "").strip()[:limit]


def _website(value: Any) -> str:
    candidate = _text(value, limit=2_048)
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    return candidate if parsed.scheme in {"http", "https"} and parsed.hostname else ""


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def normalize_yfinance_profile(
    ticker: str,
    raw: dict[str, Any],
    *,
    retrieved_at: datetime | None = None,
) -> dict[str, Any]:
    """Map unstable provider keys into Tradility's small profile contract."""
    retrieved = (retrieved_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    city = _text(raw.get("city"), limit=200)
    state = _text(raw.get("state"), limit=200)
    country = _text(raw.get("country"), limit=200)
    location = ", ".join(part for part in (city, state, country) if part)
    canonical_name = _text(
        raw.get("longName") or raw.get("shortName") or raw.get("displayName"),
        limit=500,
    )
    description = _text(raw.get("longBusinessSummary"))
    profile = {
        "available": bool(canonical_name or description),
        "canonical_name": canonical_name,
        "description": description,
        "sector": _text(raw.get("sector") or raw.get("sectorDisp"), limit=300),
        "industry": _text(raw.get("industry") or raw.get("industryDisp"), limit=300),
        "category": _text(raw.get("category"), limit=300),
        "fund_family": _text(raw.get("fundFamily"), limit=300),
        "headquarters": location,
        "website": _website(raw.get("website")),
        "exchange": _text(raw.get("fullExchangeName") or raw.get("exchange"), limit=200),
        "employees": _positive_int(raw.get("fullTimeEmployees")),
        "entity_type": _text(raw.get("quoteType"), limit=100).lower(),
        # Yahoo's general profile does not provide a reliable, company-authored
        # mission/vision field. Keep the absence explicit; never synthesize one.
        "vision_statement": None,
        "source": PROFILE_SOURCE,
        "retrieved_at": retrieved.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_ticker": ticker.strip().upper(),
    }
    return profile


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_cache(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if payload.get("schema_version") != PROFILE_CACHE_SCHEMA:
        return {}
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict):
        return {}
    return {
        str(ticker).strip().upper(): profile
        for ticker, profile in profiles.items()
        if isinstance(profile, dict)
    }


def _write_cache(path: Path, profiles: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = {
        "schema_version": PROFILE_CACHE_SCHEMA,
        "profiles": dict(sorted(profiles.items())),
    }
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_company_profiles(
    tickers: Iterable[str],
    *,
    cache_path: Path,
    max_age_days: int = 30,
    workers: int = 4,
    now: datetime | None = None,
    fetcher: Callable[[str], dict[str, Any]] = fetch_yfinance_profile,
) -> ProfileLoadResult:
    """Load fresh cached profiles and refresh missing/stale symbols.

    A refresh failure leaves a stale cached profile usable and is counted in
    artifact metadata. The cache is replaced atomically after all requests.
    """
    if max_age_days <= 0:
        raise ValueError("profile max age must be positive")
    if workers <= 0:
        raise ValueError("profile workers must be positive")

    requested = sorted({ticker.strip().upper() for ticker in tickers if ticker.strip()})
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = current - timedelta(days=max_age_days)
    cache = _load_cache(cache_path)
    selected: dict[str, dict[str, Any]] = {}
    stale: list[str] = []
    for ticker in requested:
        cached = cache.get(ticker)
        retrieved = _parse_time(cached.get("retrieved_at")) if cached else None
        if cached and retrieved and retrieved >= cutoff:
            selected[ticker] = cached
        else:
            stale.append(ticker)

    refreshed = 0
    errors = 0

    def refresh(ticker: str) -> tuple[str, dict[str, Any]]:
        raw = fetcher(ticker)
        return ticker, normalize_yfinance_profile(ticker, raw, retrieved_at=current)

    if stale:
        with ThreadPoolExecutor(max_workers=min(workers, len(stale))) as executor:
            futures = {executor.submit(refresh, ticker): ticker for ticker in stale}
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    _, profile = future.result()
                except Exception:
                    errors += 1
                    if ticker in cache:
                        selected[ticker] = cache[ticker]
                    continue
                cache[ticker] = profile
                selected[ticker] = profile
                refreshed += 1

        _write_cache(cache_path, cache)

    return ProfileLoadResult(
        profiles=selected,
        requested=len(requested),
        cache_hits=len(requested) - len(stale),
        refreshed=refreshed,
        errors=errors,
    )
