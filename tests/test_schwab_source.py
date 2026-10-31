"""Schwab source adapter tests."""

from __future__ import annotations

import json

from tradility.sources import schwab


def test_token_command_accepts_json(monkeypatch):
    class Completed:
        stdout = json.dumps({"access_token": "token-value"})

    monkeypatch.setattr(schwab.subprocess, "run", lambda *_args, **_kwargs: Completed())

    result = schwab.resolve_access_token(token_command="token-helper --json")

    assert result.token == "token-value"
    assert result.source == "command"


def test_fetch_schwab_price_history(monkeypatch):
    captured = []

    def fake_request(url, **_kwargs):
        captured.append(url)
        return {
            "candles": [
                {
                    "datetime": 1735689600000,
                    "open": 100.0,
                    "high": 102.0,
                    "low": 99.0,
                    "close": 101.0,
                    "volume": 12345,
                }
            ]
        }

    monkeypatch.setattr(schwab, "_request_json", fake_request)

    results = schwab.fetch_schwab_ohlcv(
        ["AAPL"],
        access_token="secret",
        history_period="5y",
        throttle_seconds=0,
    )

    assert results["AAPL"].ok
    assert results["AAPL"].df.iloc[-1]["Close"] == 101.0
    assert "periodType=year" in captured[0]
    assert "period=5" in captured[0]
