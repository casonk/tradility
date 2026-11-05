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


def _counting_runner(monkeypatch, outcomes):
    """Replace the token command; ``outcomes`` is a list of stdout strings or exceptions."""
    calls = []

    def run(*_args, **_kwargs):
        calls.append(1)
        outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome

        class Completed:
            stdout = outcome

        return Completed()

    monkeypatch.setattr(schwab.subprocess, "run", run)
    return calls


def test_the_token_command_runs_once_however_many_callers_ask(monkeypatch):
    # Schwab rotates the refresh token on every exchange, so a quote poll must never trigger one.
    calls = _counting_runner(monkeypatch, [json.dumps({"access_token": "tok-1"})])
    results = [schwab.resolve_access_token(token_command="helper") for _ in range(25)]
    assert len(calls) == 1 and all(r.token == "tok-1" for r in results)


def test_the_token_is_requested_again_after_it_expires(monkeypatch):
    calls = _counting_runner(
        monkeypatch, [json.dumps({"access_token": "tok-1"}), json.dumps({"access_token": "tok-2"})]
    )
    clock = {"now": 1000.0}
    monkeypatch.setattr(schwab.time, "monotonic", lambda: clock["now"])
    assert schwab.resolve_access_token(token_command="helper").token == "tok-1"
    clock["now"] += schwab.TOKEN_TTL_SECONDS - 1
    assert schwab.resolve_access_token(token_command="helper").token == "tok-1"
    clock["now"] += 2
    assert schwab.resolve_access_token(token_command="helper").token == "tok-2"
    assert len(calls) == 2


def test_a_failed_command_is_not_retried_in_a_loop_but_is_retried_after_the_backoff(monkeypatch):
    failure = schwab.subprocess.CalledProcessError(1, "helper", stderr="refresh token revoked")
    calls = _counting_runner(monkeypatch, [failure, json.dumps({"access_token": "tok"})])
    clock = {"now": 5000.0}
    monkeypatch.setattr(schwab.time, "monotonic", lambda: clock["now"])
    first = schwab.resolve_access_token(token_command="helper")
    assert first.error == "Schwab token command failed: CalledProcessError" and not first.token
    for _ in range(10):  # a busy page polling must not hammer a dead credential
        assert schwab.resolve_access_token(token_command="helper").error
    assert len(calls) == 1
    clock["now"] += schwab.TOKEN_FAILURE_BACKOFF_SECONDS + 1
    assert schwab.resolve_access_token(token_command="helper").token == "tok"
    assert len(calls) == 2


def test_different_commands_do_not_share_a_token_and_nothing_secret_reaches_disk(
    monkeypatch, tmp_path
):
    _counting_runner(monkeypatch, [json.dumps({"access_token": "secret-access-token"})])
    a = schwab.resolve_access_token(token_command="helper-a")
    b = schwab.resolve_access_token(token_command="helper-b")
    assert a.token == b.token == "secret-access-token"
    assert len(schwab._token_cache) == 2
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written and all(
        "secret-access-token" not in p.read_text(errors="ignore") for p in written
    )
    assert all((p.stat().st_mode & 0o077) == 0 for p in written)  # the lock file is owner-only
