"""Schwab fundamentals and the buy/sell reads they support.

Schwab's Market Data `instruments?projection=fundamental` returns about sixty company figures.
Percentages come as percent points (a 27.6 margin is 27.6%) and a missing figure as 0.0, so a zero
ratio is treated as unknown. The signals are transparent rules, not analyst opinion or advice.
Funds and ETFs report aggregates that do not mean the same thing, so they get the figures but no
signals.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from tradility.orders import SCHWAB_BASE, http_get_json

GetJson = Callable[[str, dict[str, str]], Any]

CHUNK = 50
MIN_SIGNALS = 4

# (key shown on the card, Schwab key, label, unit)
FIGURES: tuple[tuple[str, str, str, str], ...] = (
    ("pe", "peRatio", "P/E", "x"),
    ("peg", "pegRatio", "PEG", "x"),
    ("pb", "pbRatio", "Price / book", "x"),
    ("roe", "returnOnEquity", "Return on equity", "%"),
    ("net_margin", "netProfitMarginTTM", "Net margin", "%"),
    ("gross_margin", "grossMarginTTM", "Gross margin", "%"),
    ("rev_growth", "revChangeYear", "Revenue growth (year)", "%"),
    ("eps_growth", "epsChangePercentTTM", "EPS growth (TTM)", "%"),
    ("debt_to_equity", "totalDebtToEquity", "Debt / equity", "%"),
    ("current_ratio", "currentRatio", "Current ratio", "x"),
    ("interest_coverage", "interestCoverage", "Interest coverage", "x"),
    ("dividend_yield", "dividendYield", "Dividend yield", "%"),
    ("short_float", "shortIntToFloat", "Short interest / float", "%"),
    ("beta", "beta", "Beta", ""),
    ("market_cap", "marketCap", "Market cap", "$"),
    ("eps_ttm", "epsTTM", "EPS (TTM)", "$"),
)
# a zero here is a real value (a company can pay no dividend or owe no debt); Schwab reports 0.0
# short interest for most names, so that one is read as unknown
ZERO_IS_REAL = {"dividend_yield", "debt_to_equity", "rev_growth", "eps_growth"}


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """The figures worth showing, keyed compactly; unknown ones are left out."""
    out: dict[str, Any] = {}
    for key, source, _label, _unit in FIGURES:
        number = _num(raw.get(source))
        if number is None or (number == 0.0 and key not in ZERO_IS_REAL):
            continue
        out[key] = round(number, 4)
    return out


def fetch_schwab_fundamentals(
    symbols: list[str],
    token: str,
    get: GetJson = http_get_json,
) -> dict[str, dict[str, Any]]:
    """Fundamentals per symbol; a failing chunk is skipped so one bad symbol cannot hide the rest."""
    headers = {"Authorization": f"Bearer {token}"}
    found: dict[str, dict[str, Any]] = {}
    clean = sorted({s.strip().upper() for s in symbols if s and s.strip()})
    for start in range(0, len(clean), CHUNK):
        query = urlencode(
            {"symbol": ",".join(clean[start : start + CHUNK]), "projection": "fundamental"}
        )
        try:
            reply = get(f"{SCHWAB_BASE}/marketdata/v1/instruments?{query}", headers)
        except Exception:  # noqa: BLE001 - a failing chunk must not stop the analysis
            continue
        for item in reply.get("instruments", []) if isinstance(reply, dict) else []:
            fundamental = item.get("fundamental") if isinstance(item, dict) else None
            symbol = str(item.get("symbol", "")).upper() if isinstance(item, dict) else ""
            if symbol and isinstance(fundamental, dict):
                figures = normalize(fundamental)
                if figures:
                    found[symbol] = {
                        "asset_type": str(item.get("assetType", "")),
                        "figures": figures,
                    }
    return found


def _band(value: float | None, buy: float, sell: float, *, lower_is_better: bool = False) -> int:
    """+1, 0 or -1: a value past `buy` is favourable, past `sell` unfavourable."""
    if value is None:
        return 0
    if lower_is_better:
        return 1 if value <= buy else -1 if value >= sell else 0
    return 1 if value >= buy else -1 if value <= sell else 0


def signals(figures: dict[str, Any]) -> list[dict[str, Any]]:
    """Each rule that has the figures it needs: {key, label, value, signal, score, rule}."""
    f = figures
    rules: list[tuple[str, str, float | None, int, str]] = []
    pe, peg = f.get("pe"), f.get("peg")
    if pe is not None:
        score = -1 if pe <= 0 else _band(pe, 15, 35, lower_is_better=True)
        rules.append(("pe", "Valuation (P/E)", pe, score, "buy ≤ 15, sell ≥ 35 or no earnings"))
    if peg is not None and peg > 0:
        rules.append(
            (
                "peg",
                "Growth-adjusted (PEG)",
                peg,
                _band(peg, 1, 2, lower_is_better=True),
                "buy ≤ 1, sell ≥ 2",
            )
        )
    if f.get("roe") is not None:
        rules.append(
            ("roe", "Return on equity", f["roe"], _band(f["roe"], 15, 5), "buy ≥ 15%, sell ≤ 5%")
        )
    if f.get("net_margin") is not None:
        rules.append(
            (
                "net_margin",
                "Net margin",
                f["net_margin"],
                _band(f["net_margin"], 15, 3),
                "buy ≥ 15%, sell ≤ 3%",
            )
        )
    if f.get("rev_growth") is not None:
        rules.append(
            (
                "rev_growth",
                "Revenue growth",
                f["rev_growth"],
                _band(f["rev_growth"], 10, 0),
                "buy ≥ 10%, sell ≤ 0%",
            )
        )
    if f.get("eps_growth") is not None:
        rules.append(
            (
                "eps_growth",
                "Earnings growth",
                f["eps_growth"],
                _band(f["eps_growth"], 10, -10),
                "buy ≥ 10%, sell ≤ -10%",
            )
        )
    if f.get("debt_to_equity") is not None and f["debt_to_equity"] > 0:
        rules.append(
            (
                "debt_to_equity",
                "Leverage (debt / equity)",
                f["debt_to_equity"],
                _band(f["debt_to_equity"], 50, 150, lower_is_better=True),
                "buy ≤ 50%, sell ≥ 150%",
            )
        )
    if f.get("current_ratio") is not None:
        rules.append(
            (
                "current_ratio",
                "Liquidity (current ratio)",
                f["current_ratio"],
                _band(f["current_ratio"], 1.5, 1.0),
                "buy ≥ 1.5, sell ≤ 1",
            )
        )
    if f.get("interest_coverage") is not None:
        rules.append(
            (
                "interest_coverage",
                "Interest coverage",
                f["interest_coverage"],
                _band(f["interest_coverage"], 8, 2),
                "buy ≥ 8x, sell ≤ 2x",
            )
        )
    if f.get("short_float") is not None:
        rules.append(
            (
                "short_float",
                "Short interest",
                f["short_float"],
                _band(f["short_float"], 3, 10, lower_is_better=True) if f["short_float"] > 0 else 0,
                "buy ≤ 3% of float, sell ≥ 10%",
            )
        )
    names = {1: "buy", 0: "neutral", -1: "sell"}
    return [
        {"key": k, "label": label, "value": v, "signal": names[s], "score": s, "rule": rule}
        for k, label, v, s, rule in rules
    ]


def fundamental_rating(read: list[dict[str, Any]]) -> tuple[str, int, int]:
    """(rating, score, count): the share of rules that lean one way, needing MIN_SIGNALS of them."""
    count = len(read)
    score = sum(item["score"] for item in read)
    if count < MIN_SIGNALS:
        return "unavailable", score, count
    ratio = score / count
    if ratio >= 0.5:
        return "strong_buy", score, count
    if ratio >= 0.2:
        return "buy", score, count
    if ratio <= -0.5:
        return "strong_sell", score, count
    if ratio <= -0.2:
        return "sell", score, count
    return "hold", score, count


def record_fields(entry: dict[str, Any]) -> dict[str, Any]:
    """What a ticker record gets: the figures, and signals for companies (not funds)."""
    out: dict[str, Any] = {"fundamentals": entry["figures"], "fundamentals_source": "schwab"}
    if entry.get("asset_type", "").upper() == "EQUITY":
        read = signals(entry["figures"])
        rating, score, count = fundamental_rating(read)
        out.update(
            {
                "fundamental_signals": read,
                "fundamental_rating": rating,
                "fundamental_score": score,
                "fundamental_signal_count": count,
            }
        )
    return out
