"""Tests for qualitative profile normalization and caching."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from tradility.profiles import load_company_profiles, normalize_yfinance_profile


def _raw_profile() -> dict:
    return {
        "longName": "Example Systems, Inc.",
        "longBusinessSummary": "Example Systems builds durable test fixtures.",
        "sector": "Technology",
        "industry": "Software—Infrastructure",
        "city": "Pittsburgh",
        "state": "PA",
        "country": "United States",
        "website": "https://example.test/about",
        "fullExchangeName": "NasdaqGS",
        "fullTimeEmployees": 1234,
        "quoteType": "EQUITY",
    }


def test_normalize_profile_keeps_sourced_fields_and_does_not_invent_vision():
    profile = normalize_yfinance_profile(
        "EXM",
        _raw_profile(),
        retrieved_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
    )

    assert profile["canonical_name"] == "Example Systems, Inc."
    assert profile["description"].startswith("Example Systems builds")
    assert profile["sector"] == "Technology"
    assert profile["industry"] == "Software—Infrastructure"
    assert profile["headquarters"] == "Pittsburgh, PA, United States"
    assert profile["employees"] == 1234
    assert profile["entity_type"] == "equity"
    assert profile["vision_statement"] is None
    assert profile["source"] == "yfinance/yahoo-finance"
    assert profile["retrieved_at"] == "2026-11-01T00:00:00Z"


def test_normalize_profile_rejects_unsafe_website_scheme():
    raw = _raw_profile()
    raw["website"] = "javascript:alert(1)"

    assert normalize_yfinance_profile("EXM", raw)["website"] == ""


def test_profile_cache_refreshes_once_then_reuses_fresh_entry(tmp_path):
    cache = tmp_path / "profiles.json"
    calls: list[str] = []
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)

    def fetcher(ticker: str) -> dict:
        calls.append(ticker)
        return _raw_profile()

    first = load_company_profiles(["exm", "EXM"], cache_path=cache, now=now, fetcher=fetcher)
    second = load_company_profiles(
        ["EXM"], cache_path=cache, now=now + timedelta(days=29), fetcher=fetcher
    )

    assert calls == ["EXM"]
    assert first.refreshed == 1
    assert first.cache_hits == 0
    assert second.refreshed == 0
    assert second.cache_hits == 1
    assert second.profiles["EXM"]["canonical_name"] == "Example Systems, Inc."
    assert json.loads(cache.read_text())["schema_version"] == 1


def test_stale_profile_remains_available_when_refresh_fails(tmp_path):
    cache = tmp_path / "profiles.json"
    old = datetime(2026, 9, 1, tzinfo=timezone.utc)
    initial = load_company_profiles(
        ["EXM"], cache_path=cache, now=old, fetcher=lambda _ticker: _raw_profile()
    )

    def failing_fetcher(_ticker: str) -> dict:
        raise RuntimeError("provider unavailable")

    result = load_company_profiles(
        ["EXM"],
        cache_path=cache,
        now=datetime(2026, 11, 1, tzinfo=timezone.utc),
        fetcher=failing_fetcher,
    )

    assert initial.refreshed == 1
    assert result.errors == 1
    assert result.refreshed == 0
    assert result.profiles["EXM"]["canonical_name"] == "Example Systems, Inc."
    assert result.profiles["EXM"]["retrieved_at"] == "2026-09-01T00:00:00Z"
