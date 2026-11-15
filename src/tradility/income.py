"""Read-only dividend and interest history from Schwab, and attributing it to holdings.

GET requests only. Schwab reports a dividend or interest payment as a ``DIVIDEND_OR_INTEREST``
transaction whose only item is the dollar amount: it carries no ticker symbol, just the security's
name abbreviated the way Schwab prints it (``TEXAS INSTRS INC``, ``AIR PRODS & CHEMS INC``).
:func:`match_ticker` therefore matches that name against the company names Tradility already has,
and says so (``None``) rather than guessing when the name fits no holding or fits several.

Interest on cash and money-market funds belongs to no position and is kept apart as ``interest``.
Account numbers are cut to their last four digits and credentials never leave the token command.
"""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from tradility._compat import UTC
from tradility.orders import SCHWAB_BASE, GetJson, http_get_json, write_private
from tradility.sources.schwab import resolve_access_token

LOOKBACK_DAYS = 365
INTEREST_MARKERS = ("MONEY", "SCHWAB1 INT", "BANK INT", "CREDIT INT", "INTEREST")

# words that tell a company's name nothing: legal forms, share-class and depositary boilerplate
NOISE = {
    "INC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED", "CLASS", "CL", "COM", "THE",
    "PLC", "NEW", "DEL", "NV", "SA", "AG", "ADR", "ADS", "ORD", "SHS", "SHARES", "SHARE", "OF",
    "AND", "LLC", "LP", "HLDG", "SPONSORED", "REPS", "EQUIT", "EQUITY", "STOCK", "COMMON",
    "TRUST", "FUND", "ETF", "INDEX", "F", "UNSPONSORED",
}  # fmt: skip
MIN_MARGIN = 0.5  # the best match must beat the runner-up by this much to be believed


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def kind_of(description: str) -> str:
    """``interest`` for cash and money-market interest, else ``dividend``."""
    upper = description.upper()
    return "interest" if any(marker in upper for marker in INTEREST_MARKERS) else "dividend"


def schwab_income(raw: dict[str, Any], account: str) -> dict[str, Any] | None:
    amount = _num(raw.get("netAmount"))
    if amount is None or str(raw.get("status", "VALID")).upper() != "VALID":
        return None
    description = str(raw.get("description", "")).strip()
    symbol = ""
    for item in raw.get("transferItems") or []:
        instrument = item.get("instrument") or {}
        if instrument.get("assetType") not in (None, "CURRENCY") and instrument.get("symbol"):
            symbol = str(instrument["symbol"]).upper()
            break
    return {
        "broker": "schwab",
        "account": account,
        "date": str(raw.get("tradeDate") or raw.get("time") or "")[:10],
        "amount": amount,
        "kind": kind_of(description),
        "description": description[:80],
        "symbol": symbol,
        "qualified": bool(raw.get("qualifiedDividend")),
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
                "types": "DIVIDEND_OR_INTEREST",
            }
        )
        page = get(f"{SCHWAB_BASE}/trader/v1/accounts/{digest}/transactions?{query}", headers)
        mask = str(account.get("accountNumber", ""))[-4:]
        for raw in page if isinstance(page, list) else []:
            if isinstance(raw, dict) and (row := schwab_income(raw, mask)):
                out.append(row)
    return sorted(out, key=lambda r: (r["date"], r["description"]))


def collect(
    get: GetJson = http_get_json,
    env: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The income export; a failing source is recorded by class name only."""
    env = os.environ if env is None else env
    now = now or datetime.now(UTC)
    token = resolve_access_token(
        env.get("TRADILITY_SCHWAB_ACCESS_TOKEN", ""),
        env.get("TRADILITY_SCHWAB_TOKEN_COMMAND", ""),
    )
    if token.error:
        return {
            "version": 1,
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sources": {"schwab": {"ok": False, "error": token.error}},
            "income": [],
        }
    try:
        rows = fetch_schwab(token.token, get, now)
        source: dict[str, Any] = {"ok": True, "rows": len(rows)}
    except (HTTPError, URLError, OSError, ValueError) as exc:
        rows, source = [], {"ok": False, "error": f"request failed: {type(exc).__name__}"}
    return {
        "version": 1,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": {"schwab": source},
        "income": rows,
    }


# ── attributing a payment to a holding ───────────────────────────────────────────────────────


def name_tokens(text: str) -> list[str]:
    """The meaningful upper-case words of a company or fund name."""
    words = re.sub(r"[^A-Z0-9 ]", " ", str(text).upper().replace("&", " ")).split()
    return [w for w in words if w not in NOISE and not w.isdigit() and len(w) > 1]


def _abbreviates(short: str, full: str) -> bool:
    """``SVC`` for ``SERVICE``, ``INSTRS`` for ``INSTRUMENTS``: same first letter, letters in order."""
    if short == full or (len(short) >= 3 and full.startswith(short)):
        return True
    if len(short) < 3 or short[0] != full[0] or len(short) > len(full):
        return False
    letters = iter(full)
    return all(c in letters for c in short)


def match_score(description: list[str], name: list[str]) -> float | None:
    """How well a Schwab description fits a name, or None when a word of it has no home there."""
    if not description or not name:
        return None
    pool, total = list(name), 0.0
    for word in description:
        hit = next((n for n in pool if n == word), None)
        if hit is not None:
            total += 2
        elif (hit := next((n for n in pool if _abbreviates(word, n)), None)) is not None:
            total += 1
        elif len(word) >= 3 and word == "".join(n[0] for n in name)[: len(word)]:
            total += 1.5  # an acronym: IBM for International Business Machines
            continue
        else:
            return None
        pool.remove(hit)
    return total - 0.3 * len(pool)  # words of the name left unexplained count against it


def match_ticker(description: str, names: dict[str, str]) -> str | None:
    """The one ticker whose name fits ``description`` clearly better than any other, else None."""
    words = name_tokens(description)
    scored = sorted(
        (
            (score, ticker)
            for ticker, name in names.items()
            if (score := match_score(words, name_tokens(name))) is not None
        ),
        reverse=True,
    )
    if not scored or (len(scored) > 1 and scored[0][0] - scored[1][0] < MIN_MARGIN):
        return None
    return scored[0][1]


def attribute(
    rows: list[dict[str, Any]],
    names: dict[str, str],
    lookup: Callable[[str, str], str | None] = match_ticker,
) -> list[dict[str, Any]]:
    """Each dividend row with a ``ticker`` (or None); interest rows are left for the caller."""
    cache: dict[str, str | None] = {}
    out = []
    for row in rows:
        if row.get("kind") == "interest":
            out.append({**row, "ticker": None})
            continue
        symbol = str(row.get("symbol", "")).upper()
        if symbol and symbol in names:
            out.append({**row, "ticker": symbol})
            continue
        description = str(row.get("description", ""))
        if description not in cache:
            cache[description] = lookup(description, names)
        out.append({**row, "ticker": cache[description]})
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch read-only dividend and interest history.")
    parser.add_argument("--output", default="exports/tradility-income.json", metavar="PATH")
    args = parser.parse_args(argv)
    result = collect()
    info = result["sources"]["schwab"]
    if not info["ok"]:
        print(f"schwab: {info['error']}")
        return 1  # keep the previous export rather than replacing it with an empty one
    write_private(Path(args.output), result)
    print(f"schwab: {info['rows']} dividend and interest payments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
