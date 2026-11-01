"""Read-only deposit, withdrawal and transfer history at Schwab and Binance.US.

GET requests only. Schwab's transaction history is filtered to cash-movement types (ACH, wire,
journal and cash receipts and disbursements); Binance.US reports deposits and withdrawals in
90-day windows. Account numbers are cut to their last four digits and a withdrawal address to its
last six characters. Credentials come from the order reader's hand-off commands and are never
returned.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from tradility.accounts import _num
from tradility.orders import (
    BINANCE_BASE,
    SCHWAB_BASE,
    GetJson,
    binance_credentials,
    http_get_json,
    sign,
)
from tradility.sources.schwab import resolve_access_token

SCHWAB_CASH_TYPES = "ACH_RECEIPT,ACH_DISBURSEMENT,CASH_RECEIPT,CASH_DISBURSEMENT,ELECTRONIC_FUND,WIRE_IN,WIRE_OUT,JOURNAL"
BINANCE_WINDOW_DAYS = 90
LOOKBACK_DAYS = 365


def schwab_transfer(raw: dict[str, Any], account: str) -> dict[str, Any] | None:
    amount = _num(raw.get("netAmount"))
    if amount is None:
        return None
    return {
        "broker": "schwab",
        "account": account,
        "date": str(raw.get("tradeDate") or raw.get("time") or "")[:10],
        "direction": "in" if amount > 0 else "out",
        "amount": abs(amount),
        "asset": "USD",
        "kind": str(raw.get("type", "")).replace("_", " ").lower(),
        "status": str(raw.get("status", "")).lower(),
        "note": str(raw.get("description", ""))[:80],
    }


def fetch_schwab(token: str, get: GetJson, now: datetime) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"}
    accounts = get(f"{SCHWAB_BASE}/trader/v1/accounts/accountNumbers", headers)
    out: list[dict[str, Any]] = []
    for account in accounts if isinstance(accounts, list) else []:
        digest = str(account.get("hashValue", ""))
        if not digest:
            continue
        query = urlencode(
            {
                "startDate": (now - timedelta(days=LOOKBACK_DAYS)).strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                ),
                "endDate": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "types": SCHWAB_CASH_TYPES,
            }
        )
        page = get(f"{SCHWAB_BASE}/trader/v1/accounts/{digest}/transactions?{query}", headers)
        mask = str(account.get("accountNumber", ""))[-4:]
        for raw in page if isinstance(page, list) else []:
            if isinstance(raw, dict) and (row := schwab_transfer(raw, mask)):
                out.append(row)
    return out


def binance_transfers(
    deposits: list[dict[str, Any]], withdrawals: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out = []
    for d in deposits:
        if (amount := _num(d.get("amount"))) is None:
            continue
        millis = _num(d.get("insertTime")) or 0
        out.append(
            {
                "broker": "binance",
                "account": "",
                "date": (
                    datetime.fromtimestamp(millis / 1000, UTC).date().isoformat() if millis else ""
                ),
                "direction": "in",
                "amount": amount,
                "asset": str(d.get("coin", "")),
                "kind": f"deposit ({d.get('network', '')})".strip(),
                "status": {0: "pending", 1: "credited", 6: "credited", 2: "rejected"}.get(
                    d.get("status"), str(d.get("status"))
                ),
                "note": "",
            }
        )
    for w in withdrawals:
        if (amount := _num(w.get("amount"))) is None:
            continue
        applied = str(w.get("applyTime", ""))[:10]
        out.append(
            {
                "broker": "binance",
                "account": "",
                "date": applied,
                "direction": "out",
                "amount": amount,
                "asset": str(w.get("coin", "")),
                "kind": f"withdrawal ({w.get('network', '')})".strip(),
                "status": {
                    0: "email sent",
                    1: "cancelled",
                    2: "awaiting approval",
                    3: "rejected",
                    4: "processing",
                    5: "failure",
                    6: "completed",
                }.get(w.get("status"), str(w.get("status"))),
                "note": ("to …" + str(w.get("address", ""))[-6:]) if w.get("address") else "",
            }
        )
    return out


def fetch_binance(
    key: str, secret: str, get: GetJson, now: datetime, clock=time.time
) -> list[dict[str, Any]]:
    headers = {"X-MBX-APIKEY": key}

    def signed(path: str, **params: Any) -> Any:
        query = urlencode({**params, "recvWindow": 10000, "timestamp": int(clock() * 1000)})
        return get(f"{BINANCE_BASE}{path}?{query}&signature={sign(secret, query)}", headers)

    deposits: list[dict[str, Any]] = []
    withdrawals: list[dict[str, Any]] = []
    end = now
    floor = now - timedelta(days=LOOKBACK_DAYS)
    while end > floor:
        start = max(floor, end - timedelta(days=BINANCE_WINDOW_DAYS))
        window = {
            "startTime": int(start.timestamp() * 1000),
            "endTime": int(end.timestamp() * 1000),
        }
        got = signed("/sapi/v1/capital/deposit/hisrec", **window)
        deposits += [d for d in got if isinstance(d, dict)] if isinstance(got, list) else []
        got = signed("/sapi/v1/capital/withdraw/history", **window)
        withdrawals += [w for w in got if isinstance(w, dict)] if isinstance(got, list) else []
        end = start
    return binance_transfers(deposits, withdrawals)


def collect(
    get: GetJson = http_get_json, env: dict[str, str] | None = None, now: datetime | None = None
) -> dict[str, Any]:
    env = os.environ if env is None else env
    now = now or datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    sources: dict[str, dict[str, Any]] = {}
    token = resolve_access_token(
        env.get("TRADILITY_SCHWAB_ACCESS_TOKEN", ""), env.get("TRADILITY_SCHWAB_TOKEN_COMMAND", "")
    )
    if token.error:
        sources["schwab"] = {"ok": False, "error": token.error}
    else:
        try:
            found = fetch_schwab(token.token, get, now)
            rows += found
            sources["schwab"] = {"ok": True, "transfers": len(found)}
        except Exception as exc:  # noqa: BLE001 - class name only, never the message
            sources["schwab"] = {"ok": False, "error": f"request failed: {type(exc).__name__}"}
    key, secret, error = binance_credentials(env)
    if error:
        sources["binance"] = {"ok": False, "error": error}
    else:
        try:
            found = fetch_binance(key, secret, get, now)
            rows += found
            sources["binance"] = {"ok": True, "transfers": len(found)}
        except Exception as exc:  # noqa: BLE001
            sources["binance"] = {"ok": False, "error": f"request failed: {type(exc).__name__}"}
    rows.sort(key=lambda r: r["date"], reverse=True)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "sources": sources,
        "transfers": rows,
    }
