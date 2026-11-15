"""Actual performance of the positions held, rebuilt from the broker order history.

The benchmark tables compare a window's market return with what the *current* holdings would have
returned had they been held throughout. This module answers the other question: what did the money
actually in those positions earn, given the trades made along the way.

For each held ticker and window ``[start, end]``::

    shares at start = shares now - bought in the window + sold in the window
    gain            = value now - value at start - (cost of buys - proceeds of sells)
    base            = value at start + sum(cash put in, weighted by the share of the window left)

so ``gain / base`` is a Modified Dietz return, and gains and bases add across any set of positions.
Only ``[gain, base]`` is stored per ticker; the page sums them for the portfolio or a sector.

Dividends received (from the income export, see :mod:`tradility.income`) are added to each
ticker's gain but not to its base. What it does not capture: interest on cash, fees, options,
positions fully sold before today, and tickers whose share count does not reconcile with the fills
(splits, transfers, reinvested dividends). Those are left out and counted in ``coverage``. A window
that reaches back past a broker's order history is not computed.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from tradility.analyze import RETURN_WINDOWS
from tradility.income import LOOKBACK_DAYS as INCOME_LOOKBACK_DAYS
from tradility.income import attribute

INSTITUTION_SOURCE = {"cs": "schwab", "bin": "binance"}
HISTORY_SLACK_DAYS = 7  # the same tolerance the price returns allow at the start of a series
SHARE_TOLERANCE = 1e-6


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _day(value: Any) -> date | None:
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def analysis_ticker(row: dict[str, Any], plain: set[str]) -> str:
    """The ticker the analysis uses: a coin that shares a symbol with a stock is shown as ``T-USD``."""
    ticker = str(row.get("ticker", "")).strip().upper()
    if str(row.get("asset_class", "")) == "Crypto" and ticker in plain:
        return f"{ticker}-USD"
    return ticker


def history_starts(orders: dict[str, Any]) -> dict[str, date]:
    """The earliest order date each broker's history reaches back to."""
    starts: dict[str, date] = {}
    for rows in (orders.get("orders") or {}).values():
        for order in rows:
            day = _day(order.get("date"))
            source = str(order.get("source", ""))
            if day and source and (source not in starts or day < starts[source]):
                starts[source] = day
    return starts


def _fills(orders: dict[str, Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Filled orders by (source, symbol), with Binance's ``BTC-USD`` read as ``BTC``."""
    found: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for key, rows in (orders.get("orders") or {}).items():
        for order in rows:
            if order.get("status") != "filled":
                continue
            source = str(order.get("source", ""))
            symbol = str(key).upper()
            if source == "binance" and symbol.endswith("-USD"):
                symbol = symbol[: -len("-USD")]
            found.setdefault((source, symbol), []).append(order)
    return found


def position(
    quantity_now: float,
    price_now: float,
    window_return: float | None,
    fills: Iterable[dict[str, Any]],
    start: date,
    end: date,
) -> tuple[float, float] | None:
    """``(gain, base)`` for one position over one window, or None when it cannot be worked out."""
    span = (end - start).days
    if span <= 0:
        return None
    shares = quantity_now
    flows: list[tuple[float, float]] = []  # (cash put in, share of the window remaining)
    for order in fills:
        day = _day(order.get("date"))
        quantity, price = _num(order.get("quantity")), _num(order.get("price"))
        if day is None or not quantity or price is None or not start < day <= end:
            continue
        sign = 1 if order.get("side") == "buy" else -1
        shares -= sign * quantity
        flows.append((sign * quantity * price, (end - day).days / span))
    if shares < -SHARE_TOLERANCE * max(1.0, quantity_now):
        return None  # more bought than is now held: a split, a transfer or missing history
    shares = max(shares, 0.0)
    if shares > SHARE_TOLERANCE and (window_return is None or window_return <= -100):
        return None  # held at the start but there is no start price to value it at
    start_value = 0.0
    if shares > SHARE_TOLERANCE:
        start_value = shares * price_now / (1 + window_return / 100)  # type: ignore[operator]
    gain = quantity_now * price_now - start_value - sum(cash for cash, _ in flows)
    base = start_value + sum(cash * left for cash, left in flows)
    if base <= 0:
        return None
    return round(gain, 2), round(base, 2)


def compute_actuals(
    analysis: dict[str, Any],
    holdings: list[dict[str, Any]],
    orders: dict[str, Any],
    windows: Iterable[str] | None = None,
    income: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``actuals`` section: per-ticker ``[gain, base, income]`` for each window the history allows.

    ``gain`` already includes the ``income`` (dividends received) when an income export is given.
    """
    end = _day(analysis.get("generated_at")) or date.today()
    labels = [w for w in (windows or RETURN_WINDOWS) if w in RETURN_WINDOWS]
    starts = history_starts(orders)
    fills = _fills(orders)
    plain = {
        str(r.get("ticker", "")).strip().upper()
        for r in holdings
        if r.get("asset_class") != "Crypto"
    }
    records = {t.get("ticker"): t for t in analysis.get("tickers", []) if isinstance(t, dict)}

    window_start = {w: (pd.Timestamp(end) - RETURN_WINDOWS[w]).date() for w in labels}
    payments: list[dict[str, Any]] = []
    income_from: date | None = None
    if income and (income.get("sources") or {}).get("schwab", {}).get("ok"):
        names = {
            analysis_ticker(r, plain): str(
                (records.get(analysis_ticker(r, plain)) or {})
                .get("company_profile", {})
                .get("canonical_name")
                or (records.get(analysis_ticker(r, plain)) or {}).get("name")
                or ""
            )
            for r in holdings
            if r.get("asset_class") not in ("Crypto", "Options")
            and any(a.get("institution") == "cs" for a in r.get("accounts", []))
        }
        payments = attribute(
            list(income.get("income") or []), {k: v for k, v in names.items() if v}
        )
        generated = _day(income.get("generated_at"))
        income_from = generated - timedelta(days=INCOME_LOOKBACK_DAYS) if generated else None

    def received(w: str, name: str | None = None, kind: str = "dividend") -> float:
        return sum(
            p["amount"]
            for p in payments
            if p.get("kind") == kind
            and (name is None or p.get("ticker") == name)
            and window_start[w] < (_day(p.get("date")) or date.min) <= end
        )

    def income_covers(w: str) -> bool:
        return income_from is not None and window_start[w] >= income_from - timedelta(
            days=HISTORY_SLACK_DAYS
        )

    tickers: dict[str, dict[str, list[float]]] = {}
    value_all = dict.fromkeys(labels, 0.0)
    value_ok = dict.fromkeys(labels, 0.0)
    counts_all = dict.fromkeys(labels, 0)
    counts_ok = dict.fromkeys(labels, 0)
    unreconciled: set[str] = set()

    for row in holdings:
        name = analysis_ticker(row, plain)
        record = records.get(name)
        if not record or record.get("asset_class") == "Options":
            continue
        quantity, price = _num(row.get("total_quantity")), _num(record.get("last_price"))
        if not quantity or quantity <= 0 or price is None or price <= 0:
            continue  # a price rounded to zero (a very small coin) cannot value the position
        sources = {
            INSTITUTION_SOURCE.get(str(a.get("institution", "")), "")
            for a in row.get("accounts", [])
        } - {""}
        symbol = str(row.get("ticker", "")).strip().upper()
        own = [o for source in sources for o in fills.get((source, symbol), [])]
        for w in labels:
            value_all[w] += quantity * price
            counts_all[w] += 1
            earliest = max((starts[s] for s in sources if s in starts), default=None)
            if (
                not sources
                or earliest is None
                or window_start[w] < earliest - timedelta(days=HISTORY_SLACK_DAYS)
            ):
                continue  # this window reaches back past what the broker reports
            result = position(
                quantity, price, (record.get("returns") or {}).get(w), own, window_start[w], end
            )
            if result is None:
                unreconciled.add(name)
                continue
            gain, base = result
            if income_covers(w):
                dividends = round(received(w, name), 2)
                tickers.setdefault(name, {})[w] = [round(gain + dividends, 2), base, dividends]
            else:
                tickers.setdefault(name, {})[w] = [gain, base]
            value_ok[w] += quantity * price
            counts_ok[w] += 1

    return {
        "as_of": end.isoformat(),
        "windows": {
            w: {
                "start": window_start[w].isoformat(),
                "available": counts_ok[w] > 0,
                "positions": counts_ok[w],
                "of": counts_all[w],
                "value_share": round(value_ok[w] / value_all[w], 4) if value_all[w] else None,
            }
            for w in labels
        },
        "income": {
            w: {
                "included": round(
                    sum(v[w][2] for v in tickers.values() if w in v and len(v[w]) > 2), 2
                ),
                "left_out": round(
                    received(w)
                    - sum(v[w][2] for v in tickers.values() if w in v and len(v[w]) > 2),
                    2,
                ),
                "interest": round(received(w, kind="interest"), 2),
            }
            for w in labels
            if income_covers(w)
        },
        "history_starts": {k: v.isoformat() for k, v in starts.items()},
        "unreconciled": sorted(unreconciled),
        "tickers": tickers,
        "note": (
            "Gain on the shares held, rebuilt from filled orders, plus the dividends received on "
            "them when an income export is available. Excludes interest on cash, fees, options "
            "and positions fully sold."
        ),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Replace ``path`` atomically, keeping its permissions (the export is owner-only)."""
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1, allow_nan=False)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Add actual (trade-based) performance to the analysis export."
    )
    parser.add_argument("--analysis", default="exports/tradility-analysis.json", metavar="PATH")
    parser.add_argument("--orders", default="exports/tradility-orders.json", metavar="PATH")
    parser.add_argument("--holdings", required=True, metavar="PATH", help="holdings-aggregate.json")
    parser.add_argument(
        "--income", default=None, metavar="PATH", help="dividend and interest export (optional)"
    )
    args = parser.parse_args(argv)
    analysis_path = Path(args.analysis)
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"), parse_constant=lambda _n: None)
    orders = json.loads(Path(args.orders).read_text(encoding="utf-8"))
    holdings = json.loads(Path(args.holdings).read_text(encoding="utf-8")).get("holdings", [])
    income = None
    if args.income:
        try:
            income = json.loads(Path(args.income).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            income = None  # no income export yet: the price gain alone
    analysis["actuals"] = compute_actuals(analysis, holdings, orders, income=income)
    _write_json(analysis_path, analysis)
    for window, info in analysis["actuals"]["windows"].items():
        share = info["value_share"]
        print(
            f"actuals {window}: {info['positions']}/{info['of']} positions"
            + (
                f", {share:.0%} of value"
                if share is not None and info["available"]
                else ", not available"
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
