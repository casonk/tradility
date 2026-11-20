"""Market, sector and industry benchmark returns.

The market is Vanguard's total-market ETF. Sectors can be measured three ways, all fetched so the
page can offer the choice and compare them on the real holdings: Vanguard's sector ETFs (the
default: broad, documented, cap-weighted, small and mid caps included), State Street's SPDR Select
Sector ETFs (large caps only), and Yahoo Finance's own sector indexes. Vanguard and SPDR have no
industry funds, so industries use Yahoo's industry indexes; a sector a fund family does not cover
falls back to Yahoo's sector index. Results are cached on disk so a refresh only re-downloads what
has gone stale.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

MARKET_SYMBOL = "VTI"
MARKET_NAME = "US total market (Vanguard VTI)"
BENCHMARK_CACHE_SCHEMA = 4

# No diversified index moves this far in a day; Yahoo's index series sometimes do (a sector index
# that jumps +47% and falls 30% the next day), and a return measured across such a glitch is wrong.
SUSPECT_DAILY_MOVE_PCT = 25.0

# Yahoo sector name -> Vanguard sector ETF
SECTOR_ETFS = {
    "Technology": "VGT",
    "Healthcare": "VHT",
    "Financial Services": "VFH",
    "Consumer Cyclical": "VCR",
    "Consumer Defensive": "VDC",
    "Industrials": "VIS",
    "Energy": "VDE",
    "Basic Materials": "VAW",
    "Utilities": "VPU",
    "Communication Services": "VOX",
    "Real Estate": "VNQ",
}

# State Street's SPDR Select Sector ETFs, by Yahoo sector name
SPDR_ETFS = {
    "Technology": "XLK",
    "Healthcare": "XLV",
    "Financial Services": "XLF",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Industrials": "XLI",
    "Energy": "XLE",
    "Basic Materials": "XLB",
    "Utilities": "XLU",
    "Communication Services": "XLC",
    "Real Estate": "XLRE",
}

# id -> (label, sector name -> symbol); an empty map means Yahoo's own sector index
SECTOR_SETS: dict[str, tuple[str, dict[str, str]]] = {
    "vanguard": ("Vanguard sector ETFs", SECTOR_ETFS),
    "spdr": ("SPDR Select Sector ETFs", SPDR_ETFS),
    "yahoo": ("Yahoo Finance sector indexes", {}),
}
DEFAULT_SECTOR_SET = "vanguard"

ReturnsFn = Callable[[pd.Series], dict[str, float | None]]
HistoryFn = Callable[[str], pd.Series]
SymbolFn = Callable[[str, str], str]


def yahoo_key(name: str) -> str:
    """Yahoo's URL key for a sector or industry name ("Oil & Gas E&P" -> "oil-gas-e-p")."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower().replace("&", " ")).strip("-")


def _resolve_symbol(kind: str, key: str) -> str:
    group = yf.Sector(key) if kind == "sector" else yf.Industry(key)
    return str(group.ticker.ticker)


def _history(symbol: str) -> pd.Series:
    frame = yf.Ticker(symbol).history(period="5y", auto_adjust=True)
    return frame["Close"] if "Close" in frame else pd.Series(dtype=float)


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fresh(entry: dict[str, Any], now: datetime, max_age: timedelta) -> bool:
    try:
        got = datetime.fromisoformat(str(entry["retrieved_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return False
    return bool(entry.get("returns")) and now - got < max_age


def _load_cache(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, dict) or payload.get("schema_version") != BENCHMARK_CACHE_SCHEMA:
        return {}
    return {k: v for k, v in entries.items() if isinstance(v, dict)}


def load_benchmarks(
    sectors: Iterable[str],
    industries: Iterable[str],
    *,
    cache_path: Path,
    returns_fn: ReturnsFn,
    max_age_hours: float = 24,
    workers: int = 4,
    now: datetime | None = None,
    history_fn: HistoryFn = _history,
    symbol_fn: SymbolFn = _resolve_symbol,
) -> dict[str, Any]:
    """Return ``{market, sectors, sector_sets, industries, summary}`` of benchmark returns.

    A benchmark that cannot be fetched is left out (and counted in ``summary.errors``)
    rather than failing the run; a stale cached value is reused in its place.
    """
    stamp_now = now or datetime.now(UTC)
    max_age = timedelta(hours=max_age_hours)
    cache = _load_cache(cache_path)
    wanted: dict[str, tuple[str, str, str]] = {"market": ("market", MARKET_NAME, MARKET_SYMBOL)}
    for name in sorted({str(n).strip() for n in sectors if str(n).strip()}):
        for set_id, (_label, symbols) in SECTOR_SETS.items():
            wanted[f"sector:{set_id}:{name}"] = ("sector", name, symbols.get(name, ""))
    for name in sorted({str(n).strip() for n in industries if str(n).strip()}):
        wanted[f"industry:{name}"] = ("industry", name, "")

    def build(item: tuple[str, tuple[str, str, str]]) -> tuple[str, dict[str, Any] | None]:
        cache_key, (kind, name, symbol) = item
        old = cache.get(cache_key, {})
        if _fresh(old, stamp_now, max_age):
            return cache_key, old
        try:
            symbol = symbol or old.get("symbol") or symbol_fn(kind, yahoo_key(name))
            close = history_fn(symbol)
            returns = returns_fn(close)
            biggest = float(close.dropna().pct_change().abs().max() * 100)
        except Exception:  # noqa: BLE001 - one bad index must not sink the run
            return cache_key, old if old.get("returns") else None
        if not any(v is not None for v in returns.values()):
            return cache_key, old if old.get("returns") else None
        return cache_key, {
            "name": name,
            "symbol": symbol,
            "returns": returns,
            "max_daily_move": round(biggest, 1) if biggest == biggest else None,
            "retrieved_at": _stamp(stamp_now),
        }

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        built = dict(pool.map(build, wanted.items()))

    entries = {k: v for k, v in built.items() if v}
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"schema_version": BENCHMARK_CACHE_SCHEMA, "entries": entries}, indent=1),
            encoding="utf-8",
        )
    except OSError:
        pass

    def group(prefix: str) -> dict[str, dict[str, Any]]:
        return {
            e["name"]: {
                "symbol": e["symbol"],
                "returns": e["returns"],
                **(
                    {"suspect": True}
                    if (e.get("max_daily_move") or 0) > SUSPECT_DAILY_MOVE_PCT
                    else {}
                ),
            }
            for k, e in entries.items()
            if k.startswith(prefix)
        }

    sector_sets = {
        set_id: {"label": label, "sectors": group(f"sector:{set_id}:")}
        for set_id, (label, _symbols) in SECTOR_SETS.items()
    }

    market = entries.get("market")
    return {
        "market": (
            {"name": market["name"], "symbol": market["symbol"], "returns": market["returns"]}
            if market
            else None
        ),
        "sectors": sector_sets[DEFAULT_SECTOR_SET]["sectors"],  # the default set, as before
        "sector_sets": sector_sets,
        "default_sector_set": DEFAULT_SECTOR_SET,
        "industries": group("industry:"),
        "summary": {
            "source": "yfinance/yahoo-finance",
            "requested": len(wanted),
            "available": len(entries),
            "errors": len(wanted) - len(entries),
        },
    }
