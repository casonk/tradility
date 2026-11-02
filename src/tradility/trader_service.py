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

from tradility import accounts as accounts_module
from tradility import trading as t
from tradility import transfers as transfers_module

DEFAULT_PORT = 8768
MAX_BODY = 4096
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class Service:
    ACCOUNTS_TTL = 20.0  # seconds; the page can be refreshed often without hammering the brokers

    def __init__(
        self, policy_loader=t.load_policy, audit: t.Audit | None = None, send: t.Send = t.http_send
    ) -> None:
        self.policy_loader, self.audit, self.send = policy_loader, audit or t.Audit(), send
        self.trader = t.Trader(t.Policy(), {}, self.audit)
        self.account_loader = accounts_module.collect
        self._accounts: tuple[float, dict[str, Any]] | None = None
        self.trader.accounts_loader = self._cached_accounts
        self.trader.statements_loader = self._statements
        self._fidelity_client = None
        self.transfer_loader = transfers_module.collect
        self._transfers: tuple[float, dict[str, Any]] | None = None

    def _current(self) -> t.Trader:
        policy = self.policy_loader()
        self.trader.policy = policy
        if policy.fidelity_automation != "off" and self.trader.fidelity_driver is None:
            from tradility.fidelity_client import WorkerClient

            self.trader.fidelity_driver = self._fidelity_client = WorkerClient()
        self.trader.brokers = t.build_brokers(policy, send=self.send)
        return self.trader

    def _cached_accounts(self) -> dict[str, Any]:
        """Account details including each account's holdings, cached briefly."""
        import time

        now = time.monotonic()
        if self._accounts is None or now - self._accounts[0] > self.ACCOUNTS_TTL:
            self._accounts = (now, self.account_loader())
        return self._accounts[1]

    def _statements(self) -> dict[str, Any]:
        """Holdings at institutions read from statements, from the holdings export (if one is set)."""
        import os

        path = os.environ.get("TRADILITY_HOLDINGS_FILE", "")
        return accounts_module.statement_accounts(path) if path else {}

    def _banks(self) -> list[dict[str, Any]]:
        """Bank accounts from the finance pipeline's statements, when a holdings export is set."""
        import os

        path = os.environ.get("TRADILITY_HOLDINGS_FILE", "")
        return accounts_module.bank_accounts(path) if path else []

    def accounts(self) -> dict[str, Any]:
        """Read-only account details for the page (holdings are only used to size an order)."""
        data = self._cached_accounts()
        other = self._statements()
        return {
            **data,
            "statements": list((other.get("institutions") or {}).values()),
            "banks": self._banks(),
            "accounts": [
                {k: v for k, v in a.items() if k != "holdings"} for a in data.get("accounts", [])
            ],
        }

    def transfers(self) -> dict[str, Any]:
        """Read-only deposit, withdrawal and transfer history, cached for a minute."""
        import time

        now = time.monotonic()
        if self._transfers is None or now - self._transfers[0] > 60:
            self._transfers = (now, self.transfer_loader())
        return self._transfers[1]

    def status(self) -> dict[str, Any]:
        policy = self.policy_loader()
        return {
            "enabled": policy.enabled,
            "brokers": list(policy.brokers),
            "tickers": sorted(policy.tickers),
            "per_order_usd": policy.per_order_usd,
            "daily_usd": policy.daily_usd,
            "price_band_pct": policy.price_band_pct,
            "schwab_accounts": list(policy.schwab_accounts),
            "fidelity_automation": policy.fidelity_automation,
            "fidelity_accounts": (
                self._current().fidelity_accounts() if policy.fidelity_accounts else []
            ),
            "confirmation": policy.confirmation,
            "withdrawals": {
                "enabled": policy.withdrawals_enabled,
                "per_withdrawal_usd": policy.withdraw_per_usd,
                "daily_withdrawal_usd": policy.withdraw_daily_usd,
                "spent_today": self.audit.spent_today(events=t.Trader.WITHDRAW_EVENTS),
                "destinations": [
                    {"label": a[0], "coin": a[1], "network": a[2], "to": "…" + a[3][-6:]}
                    for a in policy.withdraw_addresses
                ],
            },
            "defaults": {
                "kind": "limit",
                "time_in_force": policy.default_tif,
                "session": policy.default_session,
                "buy_offset_pct": policy.buy_offset_pct,
                "sell_offset_pct": policy.sell_offset_pct,
            },
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
            if method == "GET" and path.startswith("/holdings/"):
                return HTTPStatus.OK, {
                    "ok": True,
                    **self.trader.holdings_for(path[len("/holdings/") :]),
                }
            if method == "GET" and path == "/transfers":
                return HTTPStatus.OK, {"ok": True, **self.transfers()}
            if method == "GET" and path == "/accounts":
                return HTTPStatus.OK, {"ok": True, **self.accounts()}
            if method == "GET" and path.startswith("/quote/"):
                return HTTPStatus.OK, {
                    "ok": True,
                    **self._current().quote(path[len("/quote/") :]),
                }
            if method != "POST" or path not in {
                "/preview",
                "/confirm",
                "/cancel",
                "/withdraw/preview",
                "/withdraw/confirm",
                "/fidelity/ticket",
                "/fidelity/stage",
                "/fidelity/submit",
            }:
                return HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"}
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "send a JSON object"}
            if not isinstance(data, dict):
                return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "send a JSON object"}
            trader = self._current()
            if path == "/fidelity/stage":
                return HTTPStatus.OK, {"ok": True, **trader.fidelity_stage(data)}
            if path == "/fidelity/submit":
                return HTTPStatus.OK, {
                    "ok": True,
                    **trader.fidelity_submit(data.get("preview_id", ""), data.get("phrase", "")),
                }
            if path == "/fidelity/ticket":
                return HTTPStatus.OK, {"ok": True, **trader.fidelity_ticket(data)}
            if path == "/withdraw/preview":
                return HTTPStatus.OK, {"ok": True, **trader.withdraw_preview(data)}
            if path == "/withdraw/confirm":
                return HTTPStatus.OK, {
                    "ok": True,
                    **trader.withdraw_confirm(data.get("preview_id", ""), data.get("phrase", "")),
                }
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
                    str(data.get("account", "")),
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
