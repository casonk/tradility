"""A local HTTP service in front of ``tradility.trading``: preview, confirm and cancel orders.

It binds 127.0.0.1 only and holds the broker credentials' hand-off commands, so the web app that
shows the order form never needs them. Requests must come from the same machine with the
``X-Tradility-Trader: 1`` header and a loopback ``Host`` (which defeats DNS rebinding), and the
service answers no CORS preflight, so a page on another origin cannot drive it. The policy file is
re-read on every request: switching ``enabled`` off stops trading at once, without a restart.

    python -m tradility.trader_service [--port 8768]
"""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tradility import trading as t

DEFAULT_PORT = 8768
MAX_BODY = 4096
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class Service:
    def __init__(
        self, policy_loader=t.load_policy, audit: t.Audit | None = None, send: t.Send = t.http_send
    ) -> None:
        self.policy_loader, self.audit, self.send = policy_loader, audit or t.Audit(), send
        self.trader = t.Trader(t.Policy(), {}, self.audit)

    def _current(self) -> t.Trader:
        policy = self.policy_loader()
        self.trader.policy = policy
        self.trader.brokers = t.build_brokers(policy, send=self.send)
        return self.trader

    def status(self) -> dict[str, Any]:
        policy = self.policy_loader()
        return {
            "enabled": policy.enabled,
            "brokers": list(policy.brokers),
            "tickers": sorted(policy.tickers),
            "per_order_usd": policy.per_order_usd,
            "daily_usd": policy.daily_usd,
            "price_band_pct": policy.price_band_pct,
            "spent_today": self.audit.spent_today(),
        }

    def handle(
        self, method: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, Any]]:
        host = (
            headers.get("Host", "").rsplit(":", 1)[0]
            if not headers.get("Host", "").startswith("[")
            else "[::1]"
        )
        if host not in LOOPBACK_HOSTS or headers.get("X-Tradility-Trader") != "1":
            return HTTPStatus.FORBIDDEN, {"ok": False, "error": "not allowed"}
        try:
            if method == "GET" and path == "/status":
                return HTTPStatus.OK, {"ok": True, **self.status()}
            if method != "POST" or path not in {"/preview", "/confirm", "/cancel"}:
                return HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"}
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "send a JSON object"}
            if not isinstance(data, dict):
                return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "send a JSON object"}
            trader = self._current()
            if path == "/preview":
                return HTTPStatus.OK, {"ok": True, **trader.preview(data)}
            if path == "/confirm":
                return HTTPStatus.OK, {
                    "ok": True,
                    **trader.confirm(data.get("preview_id", ""), data.get("phrase", "")),
                }
            return HTTPStatus.OK, {
                "ok": True,
                **trader.cancel(
                    str(data.get("broker", "")),
                    str(data.get("order_id", "")),
                    data.get("phrase", ""),
                    str(data.get("symbol", "")),
                ),
            }
        except t.TradingError as exc:
            return HTTPStatus.UNPROCESSABLE_ENTITY, {"ok": False, "error": str(exc)}


class Handler(BaseHTTPRequestHandler):
    server_version = "tradility-trader"
    service: Service

    def _serve(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            status, payload = (
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"ok": False, "error": "too large"},
            )
        else:
            body = self.rfile.read(length) if length else b""
            status, payload = self.service.handle(method, self.path, dict(self.headers), body)
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self._serve("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._serve("POST")

    def log_message(self, *args) -> None:  # the audit log is the record; no request lines
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    Handler.service = Service()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
