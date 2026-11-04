"""Owner-local manual order state used by Tradility's standalone web application."""

from __future__ import annotations

import datetime
import json
import math
import os
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Any

SIDES = ("buy", "sell")
KINDS = ("market", "limit", "stop", "stop_limit")
STATUSES = ("open", "filled", "cancelled")
MAX_ORDERS_PER_TICKER = 500
MAX_NOTE = 120
LOCK = threading.Lock()


def load_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_orders(path: Path) -> dict[str, list[dict[str, Any]]]:
    orders = load_json(path).get("orders")
    return orders if isinstance(orders, dict) else {}


def save_orders(path: Path, orders: dict[str, list[dict[str, Any]]]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".manual-orders-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "orders": orders}, handle, indent=2, sort_keys=True)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        os.unlink(temporary)
        raise


def _number(value: Any, what: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} must be a number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{what} must be a positive number")
    return number


def clean_order(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("an order is an object")
    side, kind, status = raw.get("side"), raw.get("kind", "limit"), raw.get("status", "open")
    if side not in SIDES:
        raise ValueError("side must be buy or sell")
    if kind not in KINDS:
        raise ValueError("kind must be market, limit, stop or stop_limit")
    if status not in STATUSES:
        raise ValueError("status must be open, filled or cancelled")
    price = raw.get("price")
    if price in (None, ""):
        if kind != "market" or status == "filled":
            raise ValueError("price is required (only an open or cancelled market order has none)")
        cleaned_price = None
    else:
        cleaned_price = _number(price, "price")
    date = str(raw.get("date") or datetime.date.today().isoformat())
    try:
        datetime.date.fromisoformat(date)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    note = str(raw.get("note") or "").strip()
    if len(note) > MAX_NOTE or any(ord(character) < 32 for character in note):
        raise ValueError(f"note must be plain text of at most {MAX_NOTE} characters")
    return {
        "side": side,
        "kind": kind,
        "status": status,
        "quantity": _number(raw.get("quantity"), "quantity"),
        "price": cleaned_price,
        "date": date,
        "note": note,
    }


def holding_matches(ticker: str, value: Any) -> bool:
    held = str(value or "").strip().upper()
    return held == ticker or f"{held}-USD" == ticker or held == ticker.removesuffix("-USD")


def _position(ticker: str, holdings: list[Any]) -> dict[str, Any]:
    lots: list[tuple[float, float | None]] = []
    for holding in holdings:
        if not isinstance(holding, dict) or not holding_matches(ticker, holding.get("ticker")):
            continue
        accounts = holding.get("accounts") or [
            {
                "quantity": holding.get("total_quantity"),
                "cost_basis": holding.get("total_cost_basis"),
            }
        ]
        for account in accounts:
            try:
                quantity = float(account.get("quantity"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(quantity) or quantity <= 0:
                continue
            try:
                cost = float(account.get("cost_basis"))
            except (TypeError, ValueError):
                cost = None
            if cost is not None and (not math.isfinite(cost) or cost <= 0):
                cost = None
            lots.append((quantity, cost))
    if not lots:
        return {"quantity": None, "avg_entry": None, "source": None}
    quantity = sum(row[0] for row in lots)
    known = [row for row in lots if row[1] is not None]
    covered = sum(row[0] for row in known)
    return {
        "quantity": quantity,
        "avg_entry": sum(row[1] for row in known) / covered if covered else None,
        "covered_quantity": covered,
        "partial": 0 < covered < quantity - 1e-9,
        "source": "holdings",
    }


def _synced(ticker: str, export: dict[str, Any]) -> list[dict[str, Any]]:
    found = []
    for symbol, rows in (export.get("orders") or {}).items():
        if holding_matches(ticker, symbol) and isinstance(rows, list):
            found.extend(row for row in rows if isinstance(row, dict) and row.get("source"))
    return found


def summary(
    ticker: str,
    manual: dict[str, list[dict[str, Any]]],
    holdings: list[Any],
    export: dict[str, Any],
) -> dict[str, Any]:
    mine = manual.get(ticker, [])
    position = _position(ticker, holdings)
    if position["avg_entry"] is None:
        buys = [row for row in mine if row.get("side") == "buy" and row.get("status") == "filled"]
        quantity = sum(row["quantity"] for row in buys)
        if quantity:
            position = {
                **position,
                "avg_entry": sum(row["quantity"] * row["price"] for row in buys) / quantity,
                "source": "orders",
            }
    rows = [{**row, "source": "manual"} for row in mine] + _synced(ticker, export)
    sources = export.get("sources") or {}
    return {
        "ok": True,
        "ticker": ticker,
        "position": position,
        "orders": sorted(
            sorted(rows, key=lambda row: str(row.get("date", "")), reverse=True),
            key=lambda row: row.get("status") != "open",
        ),
        "synced": {
            "generated_at": export.get("generated_at"),
            "sources": {name: bool(info.get("ok")) for name, info in sources.items()},
        },
    }


def add_order(
    ticker: str, raw: Any, orders: dict[str, list[dict[str, Any]]]
) -> dict[str, list[dict[str, Any]]]:
    mine = orders.setdefault(ticker, [])
    if len(mine) >= MAX_ORDERS_PER_TICKER:
        raise ValueError("too many orders for this ticker")
    mine.append({"id": secrets.token_hex(6), **clean_order(raw)})
    return orders


def update_order(
    ticker: str, order_id: str, raw: Any, orders: dict[str, list[dict[str, Any]]]
) -> bool:
    cleaned = clean_order(raw)
    for index, existing in enumerate(orders.get(ticker, [])):
        if existing.get("id") == order_id:
            orders[ticker][index] = {"id": order_id, **cleaned}
            return True
    return False


def delete_order(ticker: str, order_id: str, orders: dict[str, list[dict[str, Any]]]) -> bool:
    mine = orders.get(ticker, [])
    kept = [order for order in mine if order.get("id") != order_id]
    if len(kept) == len(mine):
        return False
    if kept:
        orders[ticker] = kept
    else:
        orders.pop(ticker, None)
    return True
