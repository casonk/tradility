"""Read-only order history and open orders from the brokers' APIs.

Schwab and Binance.US are asked for their orders (GET requests only: nothing here can place,
change or cancel an order) and the result is written as one owner-only JSON file that Clockwork
reads. Credentials stay outside this repository: a Schwab access token comes from the same
command ``fetch.py`` uses (``TRADILITY_SCHWAB_TOKEN_COMMAND``); Binance.US keys come from
``TRADILITY_BINANCE_API_KEY`` / ``TRADILITY_BINANCE_API_SECRET`` or a command that prints them as
JSON (``TRADILITY_BINANCE_CREDENTIAL_COMMAND``). Key, secret and token values are never logged
or written out; a failing source is reported by name and error class only.

    python -m tradility.orders --holdings HOLDINGS.json --output exports/tradility-orders.json
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tradility.sources.schwab import resolve_access_token

SCHWAB_BASE = "https://api.schwabapi.com"
BINANCE_BASE = "https://api.binance.us"
SCHWAB_WINDOW_DAYS = 60  # the orders endpoint takes at most a 60-day entered-time range
QUOTES = ("USDT", "USDC", "USD")
MAX_BINANCE_SYMBOLS = 120

Json = Any
GetJson = Callable[[str, dict[str, str]], Json]


def http_get_json(url: str, headers: dict[str, str], timeout: int = 30, retries: int = 2) -> Json:
    """GET *url* and parse the JSON reply, retrying throttling and server errors."""
    request = Request(
        url, headers={"Accept": "application/json", "User-Agent": "tradility/0.2", **headers}
    )
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt >= retries:
                raise
        except URLError:
            if attempt >= retries:
                raise
        time.sleep(min(8.0, 0.5 * (2**attempt)))
    raise RuntimeError("retries exhausted")


# -- normalisation ------------------------------------------------------------------------------


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def kind_of(order_type: str) -> str:
    t = str(order_type or "").upper()
    if t == "MARKET":
        return "market"
    if t in {"STOP_LIMIT", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"}:
        return "stop_limit"
    if t.startswith("STOP") or t in {"TAKE_PROFIT", "TRAILING_STOP"}:
        return "stop"
    return "limit"


SCHWAB_OPEN = {
    "AWAITING_PARENT_ORDER",
    "AWAITING_CONDITION",
    "AWAITING_STOP_CONDITION",
    "AWAITING_MANUAL_REVIEW",
    "ACCEPTED",
    "PENDING_ACTIVATION",
    "QUEUED",
    "WORKING",
    "NEW",
    "PENDING_ACKNOWLEDGEMENT",
    "PENDING_RECALL",
    "AWAITING_RELEASE_TIME",
    "PENDING_CANCEL",
    "PENDING_REPLACE",
}


def schwab_status(status: str) -> str:
    s = str(status or "").upper()
    if s == "FILLED":
        return "filled"
    return "open" if s in SCHWAB_OPEN else "cancelled"


def _fill_price(order: dict[str, Any]) -> float | None:
    """The quantity-weighted price of an order's executions."""
    total = cost = 0.0
    for activity in order.get("orderActivityCollection") or []:
        for leg in activity.get("executionLegs") or []:
            qty, price = _float(leg.get("quantity")), _float(leg.get("price"))
            if qty and price is not None:
                total += qty
                cost += qty * price
    return cost / total if total else None


def schwab_orders(raw: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Schwab order JSON as ``{symbol: [order]}`` in Clockwork's order shape."""
    out: dict[str, list[dict[str, Any]]] = {}
    for order in raw:
        legs = order.get("orderLegCollection") or []
        if not legs or not isinstance(legs[0], dict):
            continue
        leg = legs[0]
        symbol = str((leg.get("instrument") or {}).get("symbol", "")).upper()
        instruction = str(leg.get("instruction", "")).upper()
        if not symbol or not instruction:
            continue
        status = schwab_status(order.get("status", ""))
        quantity = _float(order.get("filledQuantity")) if status == "filled" else None
        quantity = quantity or _float(order.get("quantity")) or _float(leg.get("quantity"))
        price = _fill_price(order) if status == "filled" else None
        if price is None:
            price = _float(order.get("price")) or _float(order.get("stopPrice"))
        if not quantity or (price is None and kind_of(order.get("orderType")) != "market"):
            continue
        entered = str(order.get("closeTime") or order.get("enteredTime") or "")[:10]
        out.setdefault(symbol, []).append(
            {
                "id": f"schwab-{order.get('orderId')}",
                "source": "schwab",
                "side": "buy" if instruction.startswith("BUY") else "sell",
                "kind": kind_of(order.get("orderType")),
                "status": status,
                "quantity": quantity,
                "price": price,
                "date": entered,
                "note": "",
                **({"account": order["_account"]} if order.get("_account") else {}),
            }
        )
    return out


def binance_status(status: str) -> str:
    s = str(status or "").upper()
    if s == "FILLED":
        return "filled"
    return "open" if s in {"NEW", "PARTIALLY_FILLED", "PENDING_NEW"} else "cancelled"


def ticker_for_symbol(symbol: str) -> str | None:
    """`BTCUSD` -> `BTC-USD` (the analysis ticker); None for a pair not quoted in dollars."""
    for quote in QUOTES:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[: -len(quote)]}-USD"
    return None


def binance_orders(raw: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for order in raw:
        ticker = ticker_for_symbol(str(order.get("symbol", "")).upper())
        if ticker is None:
            continue
        status = binance_status(order.get("status", ""))
        executed = _float(order.get("executedQty"))
        quantity = executed if status == "filled" and executed else _float(order.get("origQty"))
        price = _float(order.get("price")) or _float(order.get("stopPrice"))
        spent = _float(order.get("cummulativeQuoteQty"))
        if status == "filled" and executed and spent:
            price = spent / executed
        kind = kind_of(order.get("type"))
        if not quantity or (not price and kind != "market"):
            continue
        millis = _float(order.get("time")) or 0
        out.setdefault(ticker, []).append(
            {
                "id": f"binance-{order.get('orderId')}",
                "source": "binance",
                "side": str(order.get("side", "")).lower(),
                "kind": kind,
                "status": status,
                "quantity": quantity,
                "price": price or None,
                "date": (
                    datetime.fromtimestamp(millis / 1000, UTC).date().isoformat() if millis else ""
                ),
                "note": "",
            }
        )
    return out


# -- fetching -----------------------------------------------------------------------------------


def fetch_schwab(
    token: str, get: GetJson, lookback_days: int = 365, now: datetime | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Every account's orders over *lookback_days*, in 60-day windows (read-only GETs)."""
    now = now or datetime.now(UTC)
    headers = {"Authorization": f"Bearer {token}"}
    accounts = get(f"{SCHWAB_BASE}/trader/v1/accounts/accountNumbers", headers)
    raw: list[dict[str, Any]] = []
    for account in accounts if isinstance(accounts, list) else []:
        digest = str(account.get("hashValue", ""))
        if not digest:
            continue
        mask = str(account.get("accountNumber", ""))[-4:]
        end = now
        floor = now - timedelta(days=lookback_days)
        while end > floor:
            start = max(floor, end - timedelta(days=SCHWAB_WINDOW_DAYS))
            query = urlencode(
                {
                    "fromEnteredTime": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "toEnteredTime": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "maxResults": 3000,
                }
            )
            page = get(f"{SCHWAB_BASE}/trader/v1/accounts/{digest}/orders?{query}", headers)
            if isinstance(page, list):
                raw.extend({**o, "_account": mask} for o in page if isinstance(o, dict))
            end = start
    return schwab_orders(raw)


def sign(secret: str, query: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def fetch_binance(
    key: str,
    secret: str,
    symbols: list[str],
    get: GetJson,
    clock: Callable[[], float] = time.time,
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    """Open orders (all symbols) plus the history of each held symbol (signed read-only GETs).

    Returns the orders and how many held symbols Binance.US does not list (HTTP 400, "Invalid
    symbol"): coins held elsewhere have no pair there and are skipped, not treated as a failure.
    """
    headers = {"X-MBX-APIKEY": key}

    def signed(path: str, **params: str) -> Json:
        query = urlencode({**params, "recvWindow": 10000, "timestamp": int(clock() * 1000)})
        return get(f"{BINANCE_BASE}{path}?{query}&signature={sign(secret, query)}", headers)

    raw: list[dict[str, Any]] = []
    opened = signed("/api/v3/openOrders")
    if isinstance(opened, list):
        raw.extend(o for o in opened if isinstance(o, dict))
    skipped = 0
    for symbol in symbols[:MAX_BINANCE_SYMBOLS]:
        try:
            history = signed("/api/v3/allOrders", symbol=symbol, limit="1000")
        except HTTPError as exc:
            if exc.code != 400:
                raise
            skipped += 1
            continue
        if isinstance(history, list):
            raw.extend(o for o in history if isinstance(o, dict))
    unique = {(o.get("symbol"), o.get("orderId")): o for o in raw}
    return binance_orders(list(unique.values())), skipped


def held_binance_symbols(holdings: list[dict[str, Any]]) -> list[str]:
    """`BTCUSD`-style symbols for held crypto (asset class crypto); the pair Binance.US lists."""
    symbols = []
    for holding in holdings:
        if str(holding.get("asset_class", "")).lower() not in {"crypto", "cryptocurrency"}:
            continue
        base = str(holding.get("ticker", "")).upper().removesuffix("-USD")
        if base.isalnum() and f"{base}USD" not in symbols:
            symbols.append(f"{base}USD")
    return symbols


def binance_credentials(env: dict[str, str] | None = None) -> tuple[str, str, str]:
    """(key, secret, error); never includes a value in the error."""
    env = os.environ if env is None else env
    key, secret = (
        env.get("TRADILITY_BINANCE_API_KEY", ""),
        env.get("TRADILITY_BINANCE_API_SECRET", ""),
    )
    if key and secret:
        return key, secret, ""
    command = env.get("TRADILITY_BINANCE_CREDENTIAL_COMMAND", "").strip()
    if not command:
        return "", "", "Binance credentials are not configured"
    try:
        done = subprocess.run(  # noqa: S603
            shlex.split(command), check=True, capture_output=True, text=True, timeout=30
        )
        payload = json.loads(done.stdout)
        return str(payload["api_key"]).strip(), str(payload["api_secret"]).strip(), ""
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        return "", "", f"Binance credential command failed: {type(exc).__name__}"


# -- the export ---------------------------------------------------------------------------------


def collect(
    holdings: list[dict[str, Any]],
    get: GetJson = http_get_json,
    env: dict[str, str] | None = None,
    now: datetime | None = None,
    only: tuple[str, ...] = ("schwab", "binance"),
) -> dict[str, Any]:
    """Run each source; a failing one is recorded (class name only) and does not stop the other."""
    env = os.environ if env is None else env
    now = now or datetime.now(UTC)
    sources: dict[str, dict[str, Any]] = {}
    orders: dict[str, list[dict[str, Any]]] = {}

    def merge(found: dict[str, list[dict[str, Any]]]) -> int:
        for ticker, rows in found.items():
            orders.setdefault(ticker, []).extend(rows)
        return sum(len(rows) for rows in found.values())

    if "schwab" in only:
        token = resolve_access_token(
            env.get("TRADILITY_SCHWAB_ACCESS_TOKEN", ""),
            env.get("TRADILITY_SCHWAB_TOKEN_COMMAND", ""),
        )
        if token.error:
            sources["schwab"] = {"ok": False, "error": token.error}
        else:
            try:
                sources["schwab"] = {
                    "ok": True,
                    "orders": merge(fetch_schwab(token.token, get, now=now)),
                }
            except (HTTPError, URLError, OSError, ValueError) as exc:
                sources["schwab"] = {"ok": False, "error": f"request failed: {type(exc).__name__}"}
    if "binance" in only:
        key, secret, error = binance_credentials(env)
        if error:
            sources["binance"] = {"ok": False, "error": error}
        else:
            try:
                found, skipped = fetch_binance(key, secret, held_binance_symbols(holdings), get)
                sources["binance"] = {
                    "ok": True,
                    "orders": merge(found),
                    "unlisted_symbols": skipped,
                }
            except (HTTPError, URLError, OSError, ValueError) as exc:
                sources["binance"] = {"ok": False, "error": f"request failed: {type(exc).__name__}"}
    for rows in orders.values():
        rows.sort(key=lambda o: o["date"], reverse=True)
    return {
        "version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "sources": sources,
        "orders": orders,
    }


def write_private(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".orders-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch read-only order history and open orders.")
    parser.add_argument("--holdings", required=True, metavar="PATH", help="holdings-aggregate.json")
    parser.add_argument("--output", default="exports/tradility-orders.json", metavar="PATH")
    parser.add_argument(
        "--source", action="append", choices=("schwab", "binance"), help="only these"
    )
    args = parser.parse_args(argv)
    holdings = json.loads(Path(args.holdings).read_text(encoding="utf-8")).get("holdings", [])
    result = collect(holdings, only=tuple(args.source or ("schwab", "binance")))
    write_private(Path(args.output), result)
    for name, info in result["sources"].items():
        print(
            f"{name}: {info['orders']} orders" if info["ok"] else f"{name}: {info['error']}",
            file=sys.stderr,
        )
    return 0 if any(info["ok"] for info in result["sources"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
