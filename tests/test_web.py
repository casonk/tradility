"""The standalone Tradility web application owns its UI and domain-facing APIs."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("flask")

from tradility import web  # noqa: E402


@pytest.fixture
def web_client(tmp_path):
    analysis = tmp_path / "analysis.json"
    analysis.write_text(
        json.dumps(
            {
                "generated_at": "2026-11-04T12:00:00+00:00",
                "history_period": "5y",
                "tickers": [
                    {
                        "ticker": "AAPL",
                        "name": "Apple Inc.",
                        "asset_class": "Equity",
                        "fetch_status": "ok",
                        "last_price": 200,
                        "rsi_14": 40,
                        "vwap_20": 195,
                        "long_term_trend": "bullish",
                        "price_history": [{"date": "2026-11-03", "close": 200}],
                        "company_profile": {"available": True, "description": "Technology."},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    holdings = tmp_path / "holdings.json"
    holdings.write_text(
        json.dumps(
            {
                "holdings": [
                    {
                        "ticker": "AAPL",
                        "total_quantity": 2,
                        "total_cost_basis": 300,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    orders = tmp_path / "broker-orders.json"
    orders.write_text(
        json.dumps(
            {
                "generated_at": "2026-11-04T12:00:00+00:00",
                "sources": {"schwab": {"ok": True}},
                "orders": {
                    "AAPL": [
                        {
                            "id": "schwab-1",
                            "source": "schwab",
                            "side": "buy",
                            "kind": "limit",
                            "quantity": 1,
                            "price": 190,
                            "status": "open",
                            "date": "2026-11-04",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    app = web.create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-only",
            "ANALYSIS_FILE": analysis,
            "HOLDINGS_FILE": holdings,
            "ORDERS_EXPORT": orders,
            "MANUAL_ORDERS_FILE": tmp_path / "manual-orders.json",
            "TRADER_URL": "http://127.0.0.1:9",
            "REFRESH_COMMAND": "",
            "STALE_HOURS": 72,
        }
    )
    return app.test_client(), tmp_path


def csrf(client) -> str:
    return client.get("/api/tradility-analysis").get_json()["csrf_token"]


def test_standalone_page_assets_health_and_analysis_contract(web_client):
    client, _ = web_client
    page = client.get("/")
    assert page.status_code == 200
    assert b"Standalone web application" in page.data and b"clockwork" not in page.data.lower()
    assert client.get("/static/theme.css").status_code == 200
    assert client.get("/healthz").get_json()["analysis_available"] is True

    listing = client.get("/api/tradility-analysis").get_json()
    assert listing["ok"] and listing["tickers"][0]["detail_available"] is True
    assert "price_history" not in listing["tickers"][0]
    detail = client.get("/api/tradility-analysis/aapl").get_json()["ticker"]
    assert detail["price_history"][0]["close"] == 200
    assert detail["company_profile"]["description"] == "Technology."


def test_orders_merge_broker_export_and_owner_local_manual_state(web_client):
    client, tmp_path = web_client
    token = csrf(client)
    got = client.get("/api/tradility-orders/AAPL").get_json()
    assert got["position"]["avg_entry"] == 150
    assert got["orders"][0]["id"] == "schwab-1"

    made = client.post(
        "/api/tradility-orders/AAPL",
        headers={"X-CSRF-Token": token},
        json={"side": "buy", "kind": "limit", "quantity": 2, "price": 180},
    )
    assert made.status_code == 201
    manual = next(order for order in made.get_json()["orders"] if order["source"] == "manual")
    state = tmp_path / "manual-orders.json"
    assert state.stat().st_mode & 0o777 == 0o600

    updated = {key: manual[key] for key in ("side", "kind", "quantity", "price", "date", "note")}
    updated["status"] = "filled"
    assert (
        client.put(
            f"/api/tradility-orders/AAPL/{manual['id']}",
            headers={"X-CSRF-Token": token},
            json=updated,
        ).status_code
        == 200
    )
    assert (
        client.delete(
            f"/api/tradility-orders/AAPL/{manual['id']}",
            headers={"X-CSRF-Token": token},
        ).status_code
        == 200
    )


def test_mutations_need_csrf_and_optional_integrations_fail_closed(web_client):
    client, _ = web_client
    assert client.post("/api/tradility-refresh").status_code == 403
    token = csrf(client)
    refresh = client.post("/api/tradility-refresh", headers={"X-CSRF-Token": token})
    assert refresh.status_code == 503 and "TRADILITY_REFRESH_COMMAND" in refresh.get_json()["error"]
    assert client.get("/api/tradility-targets").status_code == 503
    assert client.get("/api/finances/summary").status_code == 503
    assert client.get("/api/tradility-trade/status").get_json() == {
        "available": False,
        "enabled": False,
        "ok": True,
    }


def test_bad_analysis_and_tickers_are_reported_without_leaking_paths(tmp_path):
    missing = tmp_path / "missing.json"
    app = web.create_app({"TESTING": True, "SECRET_KEY": "x", "ANALYSIS_FILE": missing})
    client = app.test_client()
    response = client.get("/api/tradility-analysis")
    assert (
        response.status_code == 404
        and response.get_json()["error"] == "Tradility analysis file not found"
    )
    assert client.get("/api/tradility-analysis/not a ticker!").status_code == 404
