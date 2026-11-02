"""Read-only account details: value, cash, buying power and margin status at Schwab and Binance.US.

GET requests only. Schwab's accounts endpoint reports each account's type (``MARGIN`` or ``CASH``),
day-trader and call flags and its balances; Binance.US reports spot balances, which are valued in
dollars at the current prices (stablecoins at one dollar). Credentials come from the same hand-off
commands as the order reader and are never returned. Account numbers are reduced to their last
four digits.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from tradility._compat import UTC
from tradility.orders import (
    BINANCE_BASE,
    SCHWAB_BASE,
    GetJson,
    binance_credentials,
    http_get_json,
    sign,
)
from tradility.sources.schwab import resolve_access_token

STABLE = {"USD", "USDT", "USDC", "BUSD", "DAI"}
DUST_USD = 0.01


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def schwab_account(item: dict[str, Any]) -> dict[str, Any]:
    """One Schwab account as a flat, display-ready record."""
    acct = item.get("securitiesAccount") or {}
    now = acct.get("currentBalances") or {}
    initial = acct.get("initialBalances") or {}
    projected = acct.get("projectedBalances") or {}
    kind = str(acct.get("type", "")).upper() or "UNKNOWN"
    margin = kind == "MARGIN"
    call = any(
        (_num(v) or 0) > 0
        for v in (now.get("maintenanceCall"), now.get("regTCall"), initial.get("maintenanceCall"))
    ) or bool(projected.get("isInCall"))
    return {
        "broker": "schwab",
        "id": str(acct.get("accountNumber", ""))[-4:],
        "label": f"Schwab …{str(acct.get('accountNumber', ''))[-4:]}",
        "type": kind,
        "margin_eligible": margin,
        "total_value": _num(now.get("liquidationValue")),
        "cash": _num(now.get("cashBalance")),
        # what a new stock order can use: Schwab's projected stock buying power, which already
        # reserves for open orders, else the current buying power (cash only for a cash account)
        "spendable": (
            _num(projected.get("stockBuyingPower"))
            if projected.get("stockBuyingPower") is not None
            else _num(now.get("buyingPower"))
        ),
        "buying_power": _num(now.get("buyingPower")),
        "available_funds": _num(now.get("availableFunds")),
        "day_trading_buying_power": _num(now.get("dayTradingBuyingPower")) if margin else None,
        "margin_balance": _num(now.get("marginBalance")) if margin else None,
        "long_market_value": _num(now.get("longMarketValue")),
        "equity_percentage": _num(now.get("equityPercentage")),
        "day_trader": bool(acct.get("isDayTrader")),
        "closing_only": bool(acct.get("isClosingOnlyRestricted")),
        "in_call": call,
        "positions": len(acct.get("positions") or []),
        # symbol -> {quantity, market_value}; used to show an order's effect, stripped from the page feed
        "holdings": schwab_holdings(acct.get("positions") or []),
    }


def schwab_holdings(positions: list[Any]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for position in positions:
        if not isinstance(position, dict):
            continue
        symbol = str((position.get("instrument") or {}).get("symbol", "")).upper()
        value = _num(position.get("marketValue"))
        if not symbol or value is None:
            continue
        qty = (_num(position.get("longQuantity")) or 0) - (_num(position.get("shortQuantity")) or 0)
        row = out.setdefault(symbol, {"quantity": 0.0, "market_value": 0.0, "cost": 0.0})
        row["quantity"] += qty
        row["market_value"] += value
        # Schwab's own average price for the position: what the shares in this account cost on average
        average = _num(position.get("averagePrice"))
        if average is None:
            average = _num(position.get("averageLongPrice"))
        if average is not None and qty > 0:
            row["cost"] += average * qty
    for row in out.values():
        cost = row.pop("cost")
        row["average_price"] = cost / row["quantity"] if row["quantity"] > 0 and cost > 0 else None
    return out


def fetch_schwab(token: str, get: GetJson) -> list[dict[str, Any]]:
    payload = get(
        f"{SCHWAB_BASE}/trader/v1/accounts?fields=positions", {"Authorization": f"Bearer {token}"}
    )
    return (
        [schwab_account(a) for a in payload if isinstance(a, dict)]
        if isinstance(payload, list)
        else []
    )


def binance_account(account: dict[str, Any], prices: dict[str, float]) -> dict[str, Any]:
    """Binance.US spot balances valued in dollars; unlisted or dust assets are left out of the total."""
    total = cash = 0.0
    held = 0
    holdings: dict[str, dict[str, float]] = {}
    for row in account.get("balances") or []:
        qty = (_num(row.get("free")) or 0) + (_num(row.get("locked")) or 0)
        if qty <= 0:
            continue
        asset = str(row.get("asset", "")).upper()
        price = 1.0 if asset in STABLE else prices.get(asset)
        if price is None:
            continue
        value = qty * price
        if value < DUST_USD:
            continue
        held += 1
        total += value
        if asset not in STABLE:
            holdings[f"{asset}-USD"] = {"quantity": qty, "market_value": value}
        if asset == "USD":
            cash += _num(row.get("free")) or 0
    return {
        "broker": "binance",
        "id": "binance",
        "label": "Binance.US spot",
        "type": str(account.get("accountType", "SPOT")).upper(),
        "margin_eligible": False,
        "total_value": total,
        "cash": cash,
        "spendable": cash,
        "buying_power": cash,
        "available_funds": cash,
        "can_trade": bool(account.get("canTrade")),
        # a read-only key must not be able to withdraw; the page warns if this key can
        "can_withdraw": bool(account.get("canWithdraw")),
        "positions": held,
        "holdings": holdings,
    }


def fetch_binance(key: str, secret: str, get: GetJson, clock=None) -> list[dict[str, Any]]:
    import time
    from urllib.parse import urlencode

    query = urlencode({"recvWindow": 10000, "timestamp": int((clock or time.time)() * 1000)})
    account = get(
        f"{BINANCE_BASE}/api/v3/account?{query}&signature={sign(secret, query)}",
        {"X-MBX-APIKEY": key},
    )
    tickers = get(f"{BINANCE_BASE}/api/v3/ticker/price", {})
    prices = {}
    for row in tickers if isinstance(tickers, list) else []:
        symbol = str(row.get("symbol", ""))
        if symbol.endswith("USD") and len(symbol) > 3 and (price := _num(row.get("price"))):
            prices[symbol[:-3]] = price
    return [binance_account(account, prices)] if isinstance(account, dict) else []


LIVE_INSTITUTIONS = {
    "cs",
    "bin",
}  # Schwab and Binance.US are read live; the rest come from statements
INSTITUTION_NAMES = {
    "fid": "Fidelity",
    "ej": "Edward Jones",
    "wb": "Webull",
    "cs": "Schwab",
    "bin": "Binance.US",
}


def statement_accounts(path: Any) -> dict[str, Any]:
    """Holdings at institutions that are not read live (Fidelity and the like), from the finance
    pipeline's holdings export. Their values are as of each statement date, which can be old."""
    import json
    from pathlib import Path

    try:
        payload = json.loads(Path(str(path)).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    institutions: dict[str, dict[str, Any]] = {}
    holdings: dict[str, dict[str, float]] = {}
    accounts: dict[tuple[str, str], dict[str, Any]] = {}
    for holding in payload.get("holdings", []) if isinstance(payload, dict) else []:
        symbol = str(holding.get("ticker", "")).upper()
        for acct in holding.get("accounts") or []:
            code = str(acct.get("institution", ""))
            if code in LIVE_INSTITUTIONS or not code:
                continue
            value = _num(acct.get("ending_value")) or 0.0
            # per account too (a statement account is told apart by its last digits and its type)
            mask = str(acct.get("account_mask", "")).replace("*", "")[-4:]
            key = (code, f"{mask}|{acct.get('account_subtype', '')}")
            one = accounts.setdefault(
                key,
                {
                    "broker": INSTITUTION_NAMES.get(code, code).lower().replace(" ", ""),
                    "id": mask,
                    "key": f"{mask}:{acct.get('account_subtype', '')}",
                    "label": f"{INSTITUTION_NAMES.get(code, code.upper())} …{mask}",
                    "name": str(acct.get("account_name", "")),
                    "type": str(acct.get("account_subtype", "")),
                    "total_value": 0.0,
                    "as_of": "",
                    "holdings": {},
                    "statement": True,
                },
            )
            one["total_value"] += value
            one["as_of"] = max(one["as_of"], str(acct.get("period_end", "")))
            if symbol:
                slot = one["holdings"].setdefault(symbol, {"quantity": 0.0, "market_value": 0.0})
                slot["quantity"] += _num(acct.get("quantity")) or 0.0
                slot["market_value"] += value
            row = institutions.setdefault(
                code, {"name": INSTITUTION_NAMES.get(code, code.upper()), "total": 0.0, "as_of": ""}
            )
            row["total"] += value
            row["as_of"] = max(row["as_of"], str(acct.get("period_end", "")))
            if symbol:
                slot = holdings.setdefault(symbol, {"quantity": 0.0, "market_value": 0.0})
                slot["quantity"] += _num(acct.get("quantity")) or 0.0
                slot["market_value"] += value
    if not institutions:
        return {}
    _apply_latest_balances(Path(str(path)).parent, accounts, institutions)
    return {"institutions": institutions, "holdings": holdings, "accounts": list(accounts.values())}


def _apply_latest_balances(base: Any, accounts: dict, institutions: dict) -> None:
    """Newer statements can carry only balances (PDFs, no position rows). The pipeline writes a
    per-account series next to the holdings export (`fid/timeseries-<institution>-*.csv`); where
    it reaches a later period than the position rows, its ending balance replaces the total."""
    import csv

    for (code, _), one in accounts.items():
        latest = ("", 0.0)
        for series in sorted(base.glob(f"{code}/timeseries-*.csv")):
            try:
                with series.open(encoding="utf-8", newline="") as fh:
                    for row in csv.DictReader(fh):
                        mask = str(row.get("account_mask", "")).replace("*", "")[-4:]
                        end = str(row.get("period_end", ""))
                        if mask == one["id"] and end > latest[0]:
                            latest = (end, _num(row.get("ending_balance")) or 0.0)
            except (OSError, csv.Error):
                continue
        if latest[0] > one["as_of"]:
            row = institutions[code]
            row["total"] += latest[1] - one["total_value"]
            row["as_of"] = max(row["as_of"], latest[0])
            one["total_value"], one["as_of"] = latest[1], latest[0]


def collect(get: GetJson = http_get_json, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Every account from each configured broker; a failing source is named by class only."""
    env = os.environ if env is None else env
    accounts: list[dict[str, Any]] = []
    sources: dict[str, dict[str, Any]] = {}
    token = resolve_access_token(
        env.get("TRADILITY_SCHWAB_ACCESS_TOKEN", ""), env.get("TRADILITY_SCHWAB_TOKEN_COMMAND", "")
    )
    if token.error:
        sources["schwab"] = {"ok": False, "error": token.error}
    else:
        try:
            found = fetch_schwab(token.token, get)
            accounts += found
            sources["schwab"] = {"ok": True, "accounts": len(found)}
        except Exception as exc:  # noqa: BLE001 - reported by class only, never with the message
            sources["schwab"] = {"ok": False, "error": f"request failed: {type(exc).__name__}"}
    key, secret, error = binance_credentials(env)
    if error:
        sources["binance"] = {"ok": False, "error": error}
    else:
        try:
            found = fetch_binance(key, secret, get)
            accounts += found
            sources["binance"] = {"ok": True, "accounts": len(found)}
        except Exception as exc:  # noqa: BLE001
            sources["binance"] = {"ok": False, "error": f"request failed: {type(exc).__name__}"}
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sources": sources,
        "accounts": accounts,
    }
