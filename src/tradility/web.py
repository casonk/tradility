"""Standalone loopback web application for Tradility.

Tradility owns its domain UI and APIs. Portfolio shells such as Clockwork may link or reverse
proxy this application, but they are not required to browse analysis or use the trader service.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, render_template, request, session

from tradility import web_state
from tradility.live import live_indicators
from tradility.live import today_dates as live_today

TICKER = re.compile(r"[A-Z0-9][A-Z0-9.^=-]{0,19}")
TRADER_ACTIONS = ("preview", "confirm", "cancel")


def _state_dir() -> Path:
    root = os.environ.get("XDG_STATE_HOME")
    return (Path(root) if root else Path.home() / ".local" / "state") / "tradility"


def _path(env_name: str, default: str) -> Path:
    return Path(os.environ.get(env_name, default)).expanduser()


def _ticker(value: str) -> str:
    ticker = value.strip().upper()
    if not TICKER.fullmatch(ticker):
        abort(404)
    return ticker


def _artifact_version(path: Path) -> str | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _csrf_token() -> str:
    token = session.get("_csrf_token")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


QUOTE_CACHE_SECONDS = 4.0


def _trader_call(
    app: Flask, method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode() if body is not None else None
    outbound = urllib.request.Request(
        f"{app.config['TRADER_URL']}{path}",
        data=data,
        method=method,
        headers={"X-Tradility-Trader": "1", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(outbound, timeout=45) as response:  # noqa: S310
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except ValueError:
            return exc.code, {"ok": False, "error": f"trader service error ({exc.code})"}
    except (urllib.error.URLError, OSError, ValueError):
        return 503, {"ok": False, "error": "the trader service is not running"}


def _analysis(app: Flask) -> dict[str, Any]:
    path = app.config["ANALYSIS_FILE"]
    try:
        # Older exports may hold bare NaN, which is not JSON and breaks Safari; read it as null.
        data = json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda _name: None)
    except FileNotFoundError as exc:
        raise LookupError("Tradility analysis file not found") from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Could not read analysis: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Could not read analysis: the root must be an object")
    return data


def _freshness(data: dict[str, Any], stale_hours: int) -> dict[str, Any]:
    generated_at = str(data.get("generated_at", "")).strip()
    age_seconds = None
    if generated_at:
        try:
            generated = datetime.datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            if generated.tzinfo is None:
                generated = generated.replace(tzinfo=datetime.timezone.utc)
            age_seconds = max(
                0,
                int((datetime.datetime.now(datetime.timezone.utc) - generated).total_seconds()),
            )
        except ValueError:
            pass
    return {
        "age_seconds": age_seconds,
        "stale": age_seconds is None or age_seconds > stale_hours * 3600,
        "stale_after_hours": stale_hours,
    }


def _refresh_active(app: Flask) -> bool:
    process = app.config.get("REFRESH_PROCESS")
    return process is not None and process.poll() is None


def _holdings(app: Flask) -> list[Any]:
    rows = web_state.load_json(app.config["HOLDINGS_FILE"]).get("holdings")
    return rows if isinstance(rows, list) else []


def _order_summary(app: Flask, ticker: str) -> dict[str, Any]:
    return web_state.summary(
        ticker,
        web_state.load_orders(app.config["MANUAL_ORDERS_FILE"]),
        _holdings(app),
        web_state.load_json(app.config["ORDERS_EXPORT"]),
    )


def create_app(config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("TRADILITY_WEB_SECRET") or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        ANALYSIS_FILE=_path("TRADILITY_ANALYSIS_FILE", "exports/tradility-analysis.json"),
        ORDERS_EXPORT=_path("TRADILITY_ORDERS_FILE", "exports/tradility-orders.json"),
        HOLDINGS_FILE=_path("TRADILITY_HOLDINGS_FILE", "holdings-aggregate.json"),
        MANUAL_ORDERS_FILE=_path(
            "TRADILITY_MANUAL_ORDERS_FILE", str(_state_dir() / "manual-orders.json")
        ),
        TRADER_URL=os.environ.get("TRADILITY_TRADER_URL", "http://127.0.0.1:8768").rstrip("/"),
        REFRESH_COMMAND=os.environ.get("TRADILITY_REFRESH_COMMAND", "").strip(),
        REFRESH_PROCESS=None,
        STALE_HOURS=max(1, int(os.environ.get("TRADILITY_STALE_HOURS", "72"))),
    )
    if config:
        app.config.update(config)

    @app.before_request
    def protect_mutations():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        expected, supplied = session.get("_csrf_token"), request.headers.get("X-CSRF-Token")
        if not isinstance(expected, str) or not isinstance(supplied, str):
            abort(403)
        if not secrets.compare_digest(expected, supplied):
            abort(403)
        return None

    @app.get("/")
    @app.get("/to-tradility")
    def index():
        return render_template("index.html")

    @app.get("/healthz")
    def health():
        return jsonify(
            {
                "ok": True,
                "application": "tradility",
                "analysis_available": app.config["ANALYSIS_FILE"].is_file(),
            }
        )

    @app.get("/api/tradility-analysis")
    def analysis():
        try:
            data = _analysis(app)
        except LookupError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        for record in data.get("tickers", []):
            if not isinstance(record, dict):
                continue
            history = record.pop("price_history", [])
            record.pop("live_bars", None)  # server-side input for live indicators only
            profile = record.pop("company_profile", {})
            record["detail_available"] = bool(history)
            record["profile_available"] = bool(
                isinstance(profile, dict) and profile.get("available")
            )
        data.update(
            {
                "ok": True,
                "csrf_token": _csrf_token(),
                "freshness": _freshness(data, app.config["STALE_HOURS"]),
                "refresh": {"active": _refresh_active(app), "unit": None},
                "artifact_version": _artifact_version(app.config["ANALYSIS_FILE"]),
            }
        )
        return jsonify(data)

    quote_cache: dict[str, Any] = {"at": 0.0, "payload": None}
    quote_lock = threading.Lock()

    @app.get("/api/tradility-quotes")
    def live_quotes():
        """Live prices for every analysed ticker, keyed as the page names them.

        One batch per call; results are shared for a few seconds so several open
        tabs (or a fast refresh rate) cannot flood the broker's quote limit.
        """
        with quote_lock:
            fresh = time.monotonic() - quote_cache["at"] < QUOTE_CACHE_SECONDS
            if fresh and quote_cache["payload"] is not None:
                return jsonify(quote_cache["payload"])
            try:
                data = _analysis(app)
            except (LookupError, RuntimeError) as exc:
                return jsonify({"ok": False, "error": str(exc)}), 503
            symbols: dict[str, str] = {}
            records: dict[str, dict[str, Any]] = {}
            for record in data.get("tickers", []):
                if not isinstance(record, dict) or not record.get("ticker"):
                    continue
                name = str(record["ticker"]).upper()
                asset = record.get("asset_class")
                if asset == "Options":
                    continue
                symbols[f"{name}-USD" if asset == "Crypto" else name] = name
                records[name] = record
            code, payload = _trader_call(
                app, "GET", f"/quotes?{urllib.parse.urlencode({'symbols': ','.join(symbols)})}"
            )
            if code != 200 or not payload.get("ok"):
                return jsonify(payload), code
            quotes = {
                symbols[sym]: price
                for sym, price in (payload.get("quotes") or {}).items()
                if sym in symbols
            }
            sessions = payload.get("sessions") or {}
            indicators: dict[str, Any] = {}
            today = live_today()
            for sym, name in symbols.items():
                price = quotes.get(name)
                if price is None:
                    continue
                fields = live_indicators(records[name], price, sessions.get(sym), today)
                if fields:
                    indicators[name] = fields
            result = {
                "ok": True,
                "as_of": payload.get("as_of"),
                "quotes": quotes,
                "indicators": indicators,
            }
            quote_cache.update(at=time.monotonic(), payload=result)
            return jsonify(result)

    @app.get("/api/tradility-analysis/<path:ticker>")
    def ticker_detail(ticker: str):
        normalized = _ticker(ticker)
        try:
            data = _analysis(app)
        except LookupError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        record = next(
            (
                row
                for row in data.get("tickers", [])
                if isinstance(row, dict)
                and str(row.get("ticker", "")).strip().upper() == normalized
            ),
            None,
        )
        if record is None:
            return jsonify({"ok": False, "error": "Ticker not found"}), 404
        record = {k: v for k, v in record.items() if k != "live_bars"}
        return jsonify(
            {
                "ok": True,
                "generated_at": data.get("generated_at"),
                "history_period": data.get("history_period"),
                "ticker": record,
            }
        )

    @app.get("/api/tradility-refresh/status")
    def refresh_status():
        return jsonify(
            {
                "ok": True,
                "artifact_version": _artifact_version(app.config["ANALYSIS_FILE"]),
                "refresh": {"active": _refresh_active(app), "unit": None},
            }
        )

    @app.post("/api/tradility-refresh")
    def refresh():
        if _refresh_active(app):
            return jsonify({"ok": True, "already_active": True}), 202
        command = app.config["REFRESH_COMMAND"]
        if not command:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Set TRADILITY_REFRESH_COMMAND to enable browser refreshes",
                    }
                ),
                503,
            )
        try:
            app.config["REFRESH_PROCESS"] = subprocess.Popen(  # noqa: S603
                shlex.split(command),
                cwd=Path.cwd(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, ValueError) as exc:
            return jsonify({"ok": False, "error": f"Could not start refresh: {exc}"}), 503
        return jsonify({"ok": True}), 202

    @app.get("/api/tradility-targets")
    def targets_unavailable():
        return jsonify({"ok": False, "error": "No portfolio-target provider is configured"}), 503

    @app.post("/api/tradility-targets/<path:_ticker>")
    def targets_write_unavailable(_ticker: str):
        return jsonify({"ok": False, "error": "No portfolio-target provider is configured"}), 503

    @app.get("/api/finances/summary")
    def finances_unavailable():
        return jsonify({"ok": False, "error": "No finances provider is configured"}), 503

    @app.get("/api/tradility-orders/<path:ticker>")
    def orders_get(ticker: str):
        return jsonify(_order_summary(app, _ticker(ticker)))

    @app.post("/api/tradility-orders/<path:ticker>")
    def orders_add(ticker: str):
        normalized = _ticker(ticker)
        try:
            with web_state.LOCK:
                orders = web_state.load_orders(app.config["MANUAL_ORDERS_FILE"])
                web_state.add_order(normalized, request.get_json(silent=True), orders)
                web_state.save_orders(app.config["MANUAL_ORDERS_FILE"], orders)
                payload = _order_summary(app, normalized)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify(payload), 201

    @app.put("/api/tradility-orders/<ticker>/<order_id>")
    def orders_update(ticker: str, order_id: str):
        normalized = _ticker(ticker)
        try:
            with web_state.LOCK:
                orders = web_state.load_orders(app.config["MANUAL_ORDERS_FILE"])
                if not web_state.update_order(
                    normalized, order_id, request.get_json(silent=True), orders
                ):
                    return jsonify({"ok": False, "error": "order not found"}), 404
                web_state.save_orders(app.config["MANUAL_ORDERS_FILE"], orders)
                payload = _order_summary(app, normalized)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify(payload)

    @app.delete("/api/tradility-orders/<ticker>/<order_id>")
    def orders_delete(ticker: str, order_id: str):
        normalized = _ticker(ticker)
        with web_state.LOCK:
            orders = web_state.load_orders(app.config["MANUAL_ORDERS_FILE"])
            if not web_state.delete_order(normalized, order_id, orders):
                return jsonify({"ok": False, "error": "order not found"}), 404
            web_state.save_orders(app.config["MANUAL_ORDERS_FILE"], orders)
            return jsonify(_order_summary(app, normalized))

    @app.get("/api/tradility-trade/status")
    def trader_status():
        code, payload = _trader_call(app, "GET", "/status")
        if code != 200 or not payload.get("ok"):
            return jsonify({"ok": True, "available": False, "enabled": False})
        return jsonify({**payload, "available": True})

    @app.get("/api/tradility-accounts")
    def accounts():
        code, payload = _trader_call(app, "GET", "/accounts")
        return jsonify(payload), code

    @app.get("/api/tradility-transfers")
    def transfers():
        code, payload = _trader_call(app, "GET", "/transfers")
        return jsonify(payload), code

    @app.get("/api/tradility-trade/holdings/<path:ticker>")
    def trader_holdings(ticker: str):
        code, payload = _trader_call(app, "GET", f"/holdings/{_ticker(ticker)}")
        return jsonify(payload), code

    @app.get("/api/tradility-trade/quote/<path:ticker>")
    def trader_quote(ticker: str):
        code, payload = _trader_call(app, "GET", f"/quote/{_ticker(ticker)}")
        return jsonify(payload), code

    @app.post("/api/tradility-trade/withdraw/<step>")
    def trader_withdraw(step: str):
        if step not in {"preview", "confirm"}:
            abort(404)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "send a JSON object"}), 400
        code, payload = _trader_call(app, "POST", f"/withdraw/{step}", body)
        return jsonify(payload), code

    @app.post("/api/tradility-trade/batch/<step>")
    def trader_batch(step: str):
        """A ladder: one preview of several limit orders, then one confirmation that places them."""
        if step not in {"preview", "confirm"}:
            abort(404)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "send a JSON object"}), 400
        code, payload = _trader_call(app, "POST", f"/batch/{step}", body)
        return jsonify(payload), code

    @app.post("/api/tradility-trade/fidelity-<step>")
    def trader_fidelity(step: str):
        path = {
            "ticket": "/fidelity/ticket",
            "stage": "/fidelity/stage",
            "submit": "/fidelity/submit",
        }.get(step)
        if path is None:
            abort(404)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "send a JSON object"}), 400
        code, payload = _trader_call(app, "POST", path, body)
        return jsonify(payload), code

    @app.post("/api/tradility-trade/<action>")
    def trader_action(action: str):
        if action not in TRADER_ACTIONS:
            abort(404)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "send a JSON object"}), 400
        code, payload = _trader_call(app, "POST", f"/{action}", body)
        return jsonify(payload), code

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the standalone Tradility web application.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback)")
    parser.add_argument("--port", type=int, default=8769, help="bind port (default: 8769)")
    parser.add_argument("--analysis-file", metavar="PATH")
    args = parser.parse_args(argv)
    config = {"ANALYSIS_FILE": Path(args.analysis_file)} if args.analysis_file else None
    create_app(config).run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
