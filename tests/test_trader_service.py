"""The local trader service: loopback only, header-gated, policy re-read per request."""

import json

import pytest

from tests.test_trading import POLICY, Fake
from tradility import trader_service as ts
from tradility import trading as t

GOOD = {"Host": "127.0.0.1:8768", "X-Tradility-Trader": "1"}


@pytest.fixture
def svc(tmp_path):
    fake = Fake()
    state = {"policy": POLICY}
    service = ts.Service(lambda: state["policy"], t.Audit(tmp_path / "a.jsonl"), send=fake)
    service.fake, service.state = fake, state
    return service


def post(svc, path, body, headers=GOOD):
    return svc.handle("POST", path, headers, json.dumps(body).encode())


def test_preview_then_confirm_over_the_service_places_one_order(svc, monkeypatch):
    monkeypatch.setenv("TRADILITY_SCHWAB_ACCESS_TOKEN", "tok")
    code, pv = post(
        svc,
        "/preview",
        {"ticker": "AAPL", "side": "buy", "kind": "limit", "quantity": 5, "price": 100},
    )
    assert code == 200 and pv["ok"] and pv["phrase"] == "BUY AAPL"
    code, done = post(svc, "/confirm", {"preview_id": pv["preview_id"], "phrase": "BUY AAPL"})
    assert code == 200 and done["placed"] and done["broker_order_id"] == "777"


def test_requests_need_the_header_and_a_loopback_host(svc):
    body = {"ticker": "AAPL", "side": "buy", "kind": "limit", "quantity": 1, "price": 100}
    for headers in (
        {"Host": "127.0.0.1:8768"},  # no header
        {"Host": "evil.example", "X-Tradility-Trader": "1"},  # DNS rebinding
        {"Host": "10.0.0.5:8768", "X-Tradility-Trader": "1"},
        {"Host": "127.0.0.1:8768", "X-Tradility-Trader": "0"},
    ):
        assert post(svc, "/preview", body, headers)[0] == 403
    assert not svc.fake.calls
    assert (
        svc.handle("GET", "/status", {"Host": "localhost:8768", "X-Tradility-Trader": "1"}, b"")[0]
        == 200
    )


def test_disabling_the_policy_stops_trading_without_a_restart(svc, monkeypatch):
    monkeypatch.setenv("TRADILITY_SCHWAB_ACCESS_TOKEN", "tok")
    body = {"ticker": "AAPL", "side": "buy", "kind": "limit", "quantity": 1, "price": 100}
    assert post(svc, "/preview", body)[0] == 200
    svc.state["policy"] = t.Policy()
    code, refused = post(svc, "/preview", body)
    assert code == 422 and "turned off" in refused["error"]


def test_status_reports_limits_and_todays_total_but_no_secrets(svc):
    code, status = svc.handle("GET", "/status", GOOD, b"")
    assert (
        code == 200
        and status["enabled"]
        and status["per_order_usd"] == 1000
        and status["spent_today"] == 0
    )
    assert "tok" not in json.dumps(status)


def test_bad_bodies_paths_and_policy_errors_are_refused_cleanly(svc):
    assert svc.handle("POST", "/preview", GOOD, b"not json")[0] == 400
    assert svc.handle("POST", "/preview", GOOD, b"[1]")[0] == 400
    assert svc.handle("POST", "/elsewhere", GOOD, b"{}")[0] == 404
    assert svc.handle("DELETE", "/preview", GOOD, b"{}")[0] == 404

    def broken():
        raise t.TradingError("trading policy must be owner-only (chmod 600)")

    svc.policy_loader = broken
    assert svc.handle("POST", "/preview", GOOD, b"{}")[0] == 422
