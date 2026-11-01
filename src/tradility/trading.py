"""Place and cancel orders at Schwab and Binance.US, behind a policy, a preview and a confirmation.

Nothing here runs unless the owner-only policy file says ``enabled = true`` (the default is off),
and nothing is ever sent without the two steps a person completes:

1. ``Trader.preview`` checks the order against the policy (broker and ticker allowlists, a
   per-order and a daily dollar cap, a price band around the last price), asks the broker to
   validate it without placing it (Schwab ``previewOrder``, Binance.US ``order/test``) and returns
   a single-use preview token plus the exact phrase to type back, such as ``BUY AAPL``.
2. ``Trader.confirm`` takes that token and phrase, re-checks the policy (including the day's total
   from the audit log), writes the attempt to the audit log, and only then sends the order. The
   order is the one that was previewed; the token cannot be reused or changed.

Every step is appended to an owner-only audit log. An order whose outcome is unknown (a timeout)
is counted against the daily cap and reported as unknown, never retried. Credentials come from the
same hand-off commands as the order reader (``TRADILITY_SCHWAB_TOKEN_COMMAND``) plus a separate
trade-enabled Binance.US pair (``TRADILITY_BINANCE_TRADE_CREDENTIAL_COMMAND``); no value is ever
logged, audited or returned.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tradility._compat import UTC, tomllib
from tradility.orders import BINANCE_BASE, SCHWAB_BASE
from tradility.sources.schwab import resolve_access_token

SIDES = ("buy", "sell")
KINDS = ("market", "limit")
# Schwab's trading sessions: regular hours, pre-market only (AM), after-hours only (PM) and the
# extended session that spans all three (SEAMLESS); and its order durations.
SESSIONS = ("normal", "extended", "am", "pm")
SESSION_API = {"normal": "NORMAL", "extended": "SEAMLESS", "am": "AM", "pm": "PM"}
TIFS = ("day", "gtc", "fok", "ioc", "end_of_week", "end_of_month", "next_end_of_month")
TIF_API = {
    "day": "DAY",
    "gtc": "GOOD_TILL_CANCEL",
    "fok": "FILL_OR_KILL",
    "ioc": "IMMEDIATE_OR_CANCEL",
    "end_of_week": "END_OF_WEEK",
    "end_of_month": "END_OF_MONTH",
    "next_end_of_month": "NEXT_END_OF_MONTH",
}
TICKER = re.compile(r"[A-Z0-9][A-Z0-9.^=-]{0,19}")
PREVIEW_TTL_SECONDS = 120
MAX_PREVIEWS = 50

Send = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], Any]]


class TradingError(Exception):
    """A refusal or failure whose message is safe to show (it never holds a credential)."""


def default_policy_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return Path(os.environ.get("TRADILITY_TRADING_POLICY", base / "tradility" / "trading.toml"))


def default_audit_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return Path(os.environ.get("TRADILITY_TRADE_AUDIT", base / "tradility" / "trade-audit.jsonl"))


# -- policy -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    enabled: bool = False
    per_order_usd: float = 0.0
    daily_usd: float = 0.0
    price_band_pct: float = 0.0
    brokers: tuple[str, ...] = ()
    tickers: frozenset[str] = frozenset()
    schwab_accounts: tuple[str, ...] = ()  # last digits of each Schwab account that may be traded
    buy_offset_pct: float = -2.5  # the order form's default limit price relative to the mark
    sell_offset_pct: float = 2.5
    default_session: str = "normal"  # what the order form starts with: "normal" or "extended"
    default_tif: str = "gtc"  # "gtc" or "day"
    # How an order is confirmed after its preview: "phrase" (type BUY AAPL) or "button" (a click).
    # Withdrawals and cancels always need their typed phrase.
    confirmation: str = "phrase"
    # One-click order shapes: (name, side, offset_pct from the mark, tif or "", session or "")
    templates: tuple[tuple[str, str, float, str, str], ...] | None = None  # None: the built-in set
    # Withdrawals are off unless this section turns them on, and only go to listed addresses.
    withdrawals_enabled: bool = False
    withdraw_per_usd: float = 0.0
    withdraw_daily_usd: float = 0.0
    # (label, coin, network, address) for each pre-approved destination
    withdraw_addresses: tuple[tuple[str, str, str, str], ...] = ()

    def check_ticker(self, ticker: str) -> None:
        if "*" not in self.tickers and ticker not in self.tickers:
            raise TradingError(f"{ticker} is not on the trading allowlist")

    def allows(self, ticker: str) -> bool:
        return "*" in self.tickers or ticker in self.tickers


DEFAULT_TEMPLATES = tuple(
    (
        f"{'Buy' if side == 'buy' else 'Sell'} {pct}% {'under' if side == 'buy' else 'over'}",
        side,
        sign * pct,
        "",
        "",
    )
    for side, sign in (("buy", -1), ("sell", 1))
    for pct in (1, 2, 4, 8)
)


def _templates(raw: Any) -> tuple[tuple[str, str, float, str, str], ...] | None:
    """The policy's ``[[templates]]``; None when absent, which means the built-in 1/2/4/8% set."""
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > 24:
        raise TradingError("trading policy templates: a list of at most 24")
    out = []
    for item in raw:
        name = str(item["name"]).strip()
        side = str(item["side"]).lower()
        offset = float(item["offset_pct"])
        tif = str(item.get("tif", "")).lower()
        session = str(item.get("session", "")).lower()
        if (
            not name
            or len(name) > 32
            or side not in SIDES
            or not math.isfinite(offset)
            or abs(offset) > 50
        ):
            raise TradingError(
                "trading policy template needs a name, a side and an offset within 50%"
            )
        if (tif and tif not in TIFS) or (session and session not in SESSIONS):
            raise TradingError("trading policy template has an unknown duration or session")
        out.append((name, side, offset, tif, session))
    return tuple(out)


def load_policy(path: Path | None = None) -> Policy:
    """The policy, or a disabled one when the file is missing; an unreadable file refuses to trade."""
    path = path or default_policy_path()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Policy()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise TradingError(f"trading policy is unreadable ({type(exc).__name__})") from exc
    if path.stat().st_mode & 0o077:
        raise TradingError("trading policy must be owner-only (chmod 600)")
    try:
        policy = Policy(
            enabled=raw.get("enabled") is True,
            per_order_usd=float(raw.get("per_order_usd", 0)),
            daily_usd=float(raw.get("daily_usd", 0)),
            price_band_pct=float(raw.get("price_band_pct", 0)),
            brokers=tuple(str(b) for b, on in (raw.get("brokers") or {}).items() if on is True),
            tickers=frozenset(str(t).upper() for t in raw.get("tickers", [])),
            schwab_accounts=tuple(
                str(a).strip()
                for a in (raw.get("schwab") or {}).get("accounts", [])
                if str(a).strip()
            ),
            buy_offset_pct=float((raw.get("defaults") or {}).get("buy_offset_pct", -2.5)),
            sell_offset_pct=float((raw.get("defaults") or {}).get("sell_offset_pct", 2.5)),
            default_session=str((raw.get("defaults") or {}).get("session", "normal")).lower(),
            default_tif=str((raw.get("defaults") or {}).get("time_in_force", "gtc")).lower(),
            confirmation=str((raw.get("defaults") or {}).get("confirmation", "phrase")).lower(),
            templates=_templates(raw.get("templates")),
            withdrawals_enabled=(raw.get("withdrawals") or {}).get("enabled") is True,
            withdraw_per_usd=float((raw.get("withdrawals") or {}).get("per_withdrawal_usd", 0)),
            withdraw_daily_usd=float((raw.get("withdrawals") or {}).get("daily_withdrawal_usd", 0)),
            withdraw_addresses=tuple(
                (
                    str(a["label"]).strip(),
                    str(a["coin"]).upper(),
                    str(a.get("network", "")),
                    str(a["address"]).strip(),
                )
                for a in (raw.get("withdrawals") or {}).get("addresses", [])
            ),
        )
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise TradingError("trading policy has a bad value") from exc
    if policy.confirmation not in ("phrase", "button"):
        raise TradingError("trading policy defaults: confirmation is phrase or button")
    if policy.default_session not in SESSIONS or policy.default_tif not in TIFS:
        raise TradingError(
            "trading policy defaults: session is normal or extended, time_in_force is gtc or day"
        )
    if not all(
        math.isfinite(v) and v >= 0 for v in (policy.withdraw_per_usd, policy.withdraw_daily_usd)
    ):
        raise TradingError("withdrawal limits must be finite and not negative")
    if not all(math.isfinite(v) for v in (policy.buy_offset_pct, policy.sell_offset_pct)):
        raise TradingError("trading policy default offsets must be finite")
    if not all(
        math.isfinite(v) and v >= 0
        for v in (policy.per_order_usd, policy.daily_usd, policy.price_band_pct)
    ):
        raise TradingError("trading policy limits must be finite and not negative")
    return policy


# -- the order ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Order:
    ticker: str
    side: str
    kind: str
    quantity: float
    price: float | None = None
    account: str = ""  # a Schwab account's last digits; Binance.US has one account
    session: str = "normal"  # "extended" also trades pre-market and after hours (Schwab SEAMLESS)
    tif: str = "gtc"  # "gtc" rests until filled or cancelled; "day" expires at the close

    @property
    def broker(self) -> str:
        return "binance" if self.ticker.endswith("-USD") else "schwab"

    @property
    def phrase(self) -> str:
        return f"{self.side.upper()} {self.ticker}"


def parse_order(raw: Any) -> Order:
    if not isinstance(raw, dict):
        raise TradingError("an order is an object")
    ticker = str(raw.get("ticker", "")).strip().upper()
    if not TICKER.fullmatch(ticker):
        raise TradingError("a ticker is required")
    side, kind = raw.get("side"), raw.get("kind", "limit")
    if side not in SIDES:
        raise TradingError("side must be buy or sell")
    if kind not in KINDS:
        raise TradingError("kind must be market or limit")
    try:
        quantity = float(raw.get("quantity"))
    except (TypeError, ValueError) as exc:
        raise TradingError("quantity must be a number") from exc
    if not math.isfinite(quantity) or quantity <= 0:
        raise TradingError("quantity must be positive")
    price: float | None = None
    if kind == "limit":
        try:
            price = float(raw.get("price"))
        except (TypeError, ValueError) as exc:
            raise TradingError("a limit order needs a price") from exc
        if not math.isfinite(price) or price <= 0:
            raise TradingError("price must be positive")
    session = str(raw.get("session", "normal")).strip().lower()
    tif = str(raw.get("tif", "gtc")).strip().lower()
    if session not in SESSIONS:
        raise TradingError("hours must be one of " + ", ".join(SESSIONS))
    if tif not in TIFS:
        raise TradingError("duration must be one of " + ", ".join(TIFS))
    if kind == "market" and session != "normal":
        raise TradingError("a market order can only trade in regular hours; use a limit order")
    if kind == "market" and tif in {"gtc", "end_of_week", "end_of_month", "next_end_of_month"}:
        raise TradingError(
            "a market order cannot rest; choose day, fill or kill or immediate or cancel"
        )
    if session != "normal" and tif not in {"day", "gtc"}:
        raise TradingError(
            "extended, pre-market and after-hours orders can be day or good till cancelled"
        )
    return Order(
        ticker, side, kind, quantity, price, str(raw.get("account", "")).strip(), session, tif
    )


# -- audit --------------------------------------------------------------------------------------


class Audit:
    """An append-only owner-only JSON Lines log; the day's placed total is read back from it."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_audit_path()

    def write(self, event: str, **fields: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        entry = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "event": event, **fields}
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")

    def spent_today(
        self, now: datetime | None = None, events: frozenset[str] = frozenset({"placed", "unknown"})
    ) -> float:
        """Notional of every order sent today (UTC), including ones whose outcome is unknown."""
        day = (now or datetime.now(UTC)).date().isoformat()
        total = 0.0
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0.0
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("event") in events and str(entry.get("ts", "")).startswith(day):
                total += float(entry.get("notional", 0) or 0)
        return total


# -- HTTP ---------------------------------------------------------------------------------------


def http_send(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, dict[str, str], Any]:
    request = Request(
        url,
        data=body,
        method=method,
        headers={"Accept": "application/json", "User-Agent": "tradility/0.2", **headers},
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
            return response.status, dict(response.headers), json.loads(raw) if raw.strip() else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"message": raw[:200]}
        return exc.code, dict(exc.headers), payload


def run_credential_command(command: str) -> dict[str, Any]:
    try:
        done = subprocess.run(  # noqa: S603
            shlex.split(command), check=True, capture_output=True, text=True, timeout=30
        )
        payload = json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise TradingError(f"credential command failed ({type(exc).__name__})") from exc
    if not isinstance(payload, dict):
        raise TradingError("credential command returned an unexpected value")
    return payload


# -- brokers ------------------------------------------------------------------------------------


@dataclass
class Placed:
    broker_order_id: str
    detail: dict[str, Any] = field(default_factory=dict)


def _validation_text(item: dict[str, Any], fallback: str) -> str:
    """The words of one Schwab validation entry (its text is in ``activityMessage``)."""
    return str(item.get("activityMessage") or item.get("message") or fallback)


class SchwabTrader:
    name = "schwab"

    def __init__(self, token: str, send: Send) -> None:
        self.headers = {"Authorization": f"Bearer {token}"}
        self._send, self._hashes = send, {}

    def send(self, method: str, url: str, headers: dict[str, str], body: bytes | None):
        """Schwab answers 400 to a GET that declares a JSON body, so only requests with one do."""
        sent = {**headers, "Content-Type": "application/json"} if body is not None else headers
        return self._send(method, url, sent, body)

    def _account_hash(self, mask: str) -> str:
        if mask in self._hashes:
            return self._hashes[mask]
        if not mask:
            raise TradingError("choose which Schwab account to trade in")
        status, _, payload = self.send(
            "GET", f"{SCHWAB_BASE}/trader/v1/accounts/accountNumbers", self.headers, None
        )
        if status != 200 or not isinstance(payload, list):
            raise TradingError(f"Schwab accounts lookup failed (HTTP {status})")
        matches = [a for a in payload if str(a.get("accountNumber", "")).endswith(mask)]
        if len(matches) != 1:
            raise TradingError("that account did not match exactly one Schwab account")
        self._hashes[mask] = str(matches[0]["hashValue"])
        return self._hashes[mask]

    def last_price(self, ticker: str) -> float:
        query = urlencode({"symbols": ticker, "fields": "quote"})
        status, _, payload = self.send(
            "GET", f"{SCHWAB_BASE}/marketdata/v1/quotes?{query}", self.headers, None
        )
        quote = (payload.get(ticker) or {}).get("quote", {}) if isinstance(payload, dict) else {}
        price = 0.0
        for field_name in ("mark", "lastPrice"):  # the mark first, then the last trade
            try:
                price = float(quote[field_name])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(price) and price > 0:
                break
        if status != 200 or not math.isfinite(price) or price <= 0:
            raise TradingError("no current price was available for that ticker")
        return price

    @staticmethod
    def body(order: Order) -> bytes:
        payload: dict[str, Any] = {
            "orderType": "MARKET" if order.kind == "market" else "LIMIT",
            "session": SESSION_API[order.session],
            "duration": TIF_API[order.tif],
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": order.side.upper(),
                    "quantity": order.quantity,
                    "instrument": {"symbol": order.ticker, "assetType": "EQUITY"},
                }
            ],
        }
        if order.price is not None:
            payload["price"] = f"{order.price:.2f}"
        return json.dumps(payload).encode()

    def preview(self, order: Order) -> dict[str, Any]:
        url = f"{SCHWAB_BASE}/trader/v1/accounts/{self._account_hash(order.account)}/previewOrder"
        status, _, payload = self.send("POST", url, self.headers, self.body(order))
        if status not in (200, 201):
            raise TradingError(f"Schwab rejected the preview (HTTP {status})")
        result = payload.get("orderValidationResult") or {}
        rejects = [
            _validation_text(r, "rejected")
            for r in result.get("rejects") or []
            if isinstance(r, dict)
        ]
        if rejects:
            raise TradingError("Schwab would reject this order: " + "; ".join(rejects)[:300])
        warnings = [
            _validation_text(w, "")
            for w in (result.get("warns") or []) + (result.get("alerts") or [])
            if isinstance(w, dict)
        ]
        return {"warnings": [w for w in warnings if w]}

    def place(self, order: Order, client_id: str) -> Placed:
        url = f"{SCHWAB_BASE}/trader/v1/accounts/{self._account_hash(order.account)}/orders"
        status, headers, payload = self.send("POST", url, self.headers, self.body(order))
        if status not in (200, 201):
            raise TradingError(
                f"Schwab did not accept the order (HTTP {status}): {str(payload)[:200]}"
            )
        location = next((v for k, v in headers.items() if k.lower() == "location"), "")
        return Placed(location.rstrip("/").rsplit("/", 1)[-1] or "unknown")

    def cancel(self, order_id: str, account: str = "") -> None:
        if not re.fullmatch(r"\d{1,20}", order_id):
            raise TradingError("that is not a Schwab order id")
        url = f"{SCHWAB_BASE}/trader/v1/accounts/{self._account_hash(account)}/orders/{order_id}"
        status, _, payload = self.send("DELETE", url, self.headers, None)
        if status not in (200, 201, 204):
            raise TradingError(
                f"Schwab did not cancel the order (HTTP {status}): {str(payload)[:200]}"
            )


class BinanceTrader:
    name = "binance"

    def __init__(
        self, key: str, secret: str, send: Send, clock: Callable[[], float] = time.time
    ) -> None:
        self.key, self.secret, self.send, self.clock = key, secret, send, clock
        self._rules: dict[str, dict[str, Decimal]] = {}

    def rules(self, ticker: str) -> dict[str, Decimal]:
        """The pair's tick size, lot step, minimum quantity and minimum notional (public, cached)."""
        symbol = self.symbol(ticker)
        if symbol in self._rules:
            return self._rules[symbol]
        status, _, payload = self.send(
            "GET", f"{BINANCE_BASE}/api/v3/exchangeInfo?{urlencode({'symbol': symbol})}", {}, None
        )
        try:
            filters = {f["filterType"]: f for f in payload["symbols"][0]["filters"]}
            rules = {
                "tick": Decimal(str(filters["PRICE_FILTER"]["tickSize"])),
                "step": Decimal(str(filters["LOT_SIZE"]["stepSize"])),
                "min_qty": Decimal(str(filters["LOT_SIZE"]["minQty"])),
            }
            notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
            rules["min_notional"] = Decimal(str(notional.get("minNotional", "0")))
        except (KeyError, IndexError, TypeError, ValueError, ArithmeticError) as exc:
            raise TradingError(f"Binance.US does not list {symbol} (HTTP {status})") from exc
        self._rules[symbol] = rules
        return rules

    def round_price(self, ticker: str, price: float) -> float:
        """*price* moved to the nearest multiple of the pair's tick size."""
        tick = self.rules(ticker)["tick"]
        return float((Decimal(str(price)) / tick).to_integral_value(ROUND_HALF_UP) * tick)

    def check_order(self, order: Order) -> None:
        """Refuse an order the exchange's filters would reject, naming what is allowed."""
        rules = self.rules(order.ticker)
        qty = Decimal(str(order.quantity))
        if qty < rules["min_qty"] or qty % rules["step"] != 0:
            step, least = rules["step"].normalize(), rules["min_qty"].normalize()
            raise TradingError(f"quantity must be a multiple of {step:f}, at least {least:f}")
        if order.price is not None:
            price = Decimal(str(order.price))
            if price % rules["tick"] != 0:
                raise TradingError(f"price must be a multiple of {rules['tick'].normalize():f}")
            if qty * price < rules["min_notional"]:
                least = rules["min_notional"].normalize()
                raise TradingError(f"the order is below Binance.US's ${least:f} minimum")

    def _signed(self, method: str, path: str, params: dict[str, Any]) -> tuple[int, Any]:
        query = urlencode({**params, "recvWindow": 10000, "timestamp": int(self.clock() * 1000)})
        signature = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        body = f"{query}&signature={signature}".encode()
        status, _, payload = self.send(
            method,
            f"{BINANCE_BASE}{path}",
            {"X-MBX-APIKEY": self.key, "Content-Type": "application/x-www-form-urlencoded"},
            body,
        )
        return status, payload

    @staticmethod
    def symbol(ticker: str) -> str:
        return ticker.removesuffix("-USD") + "USD"

    def last_price(self, ticker: str) -> float:
        status, _, payload = self.send(
            "GET",
            f"{BINANCE_BASE}/api/v3/ticker/price?{urlencode({'symbol': self.symbol(ticker)})}",
            {},
            None,
        )
        try:
            price = float(payload["price"])
        except (KeyError, TypeError, ValueError):
            price = 0.0
        if status != 200 or not math.isfinite(price) or price <= 0:
            raise TradingError("no current price was available for that ticker")
        return price

    def params(self, order: Order, client_id: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "symbol": self.symbol(order.ticker),
            "side": order.side.upper(),
            "type": "MARKET" if order.kind == "market" else "LIMIT",
            "quantity": f"{order.quantity:.8f}".rstrip("0").rstrip("."),
            "newClientOrderId": client_id,
        }
        if order.price is not None:
            params["price"] = f"{order.price:.8f}".rstrip("0").rstrip(".")
            params["timeInForce"] = "GTC"
        return params

    def preview(self, order: Order) -> dict[str, Any]:
        status, payload = self._signed("POST", "/api/v3/order/test", self.params(order, "preview"))
        if status != 200:
            raise TradingError(
                f"Binance.US would reject this order: {str(payload.get('msg', status))[:200]}"
            )
        return {"warnings": []}

    def place(self, order: Order, client_id: str) -> Placed:
        status, payload = self._signed("POST", "/api/v3/order", self.params(order, client_id))
        if status != 200:
            raise TradingError(
                f"Binance.US did not accept the order: {str(payload.get('msg', status))[:200]}"
            )
        return Placed(str(payload.get("orderId", "unknown")))

    def cancel(self, order_id: str, symbol: str = "") -> None:
        if not re.fullmatch(r"\d{1,20}", order_id) or not re.fullmatch(r"[A-Z0-9]{5,20}", symbol):
            raise TradingError("cancelling needs the order id and its symbol")
        status, payload = self._signed(
            "DELETE", "/api/v3/order", {"symbol": symbol, "orderId": order_id}
        )
        if status != 200:
            raise TradingError(
                f"Binance.US did not cancel the order: {str(payload.get('msg', status))[:200]}"
            )


class BinanceWithdrawer(BinanceTrader):
    """Withdrawals use their own credentials (a key allowed to withdraw, ideally IP-restricted)."""

    def balance(self, coin: str) -> float:
        status, payload = self._signed("GET", "/api/v3/account", {})
        if status != 200:
            raise TradingError(f"Binance.US account lookup failed (HTTP {status})")
        for row in payload.get("balances", []):
            if str(row.get("asset", "")).upper() == coin:
                return float(row.get("free", 0) or 0)
        return 0.0

    def usd_price(self, coin: str) -> float:
        if coin in {"USD", "USDT", "USDC"}:
            return 1.0
        return self.last_price(f"{coin}-USD")

    def withdraw(self, coin: str, network: str, address: str, amount: float, client_id: str) -> str:
        params: dict[str, Any] = {
            "coin": coin,
            "address": address,
            "amount": f"{amount:.8f}".rstrip("0").rstrip("."),
            "withdrawOrderId": client_id,
        }
        if network:
            params["network"] = network
        status, payload = self._signed("POST", "/sapi/v1/capital/withdraw/apply", params)
        if status != 200:
            raise TradingError(
                f"Binance.US did not accept the withdrawal: {str(payload.get('msg', status))[:200]}"
            )
        return str(payload.get("id", "unknown"))


# -- the trader ---------------------------------------------------------------------------------


class Trader:
    """Previews and confirms orders. *brokers* maps a broker name to a factory, so credentials are
    only fetched when an order needs them."""

    def __init__(
        self,
        policy: Policy,
        brokers: dict[str, Callable[[], Any]],
        audit: Audit | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.policy, self.brokers, self.audit, self.now = policy, brokers, audit or Audit(), now
        self._previews: dict[str, tuple[Order, float, float]] = {}
        self._lock = threading.Lock()

    def _broker(self, order: Order) -> Any:
        if not self.policy.enabled:
            raise TradingError("trading is turned off in the policy")
        if order.broker not in self.policy.brokers or order.broker not in self.brokers:
            raise TradingError(f"{order.broker} is not enabled for trading")
        self.policy.check_ticker(order.ticker)
        if order.broker == "schwab" and order.account not in self.policy.schwab_accounts:
            raise TradingError("choose one of the Schwab accounts the policy allows")
        return self.brokers[order.broker]()

    accounts_loader: Callable[[], dict[str, Any]] | None = None
    statements_loader: Callable[[], dict[str, Any]] | None = (
        None  # holdings kept at other institutions
    )

    def impact(self, order: Order, last: float) -> dict[str, Any] | None:
        """What the order does to the position: dollars and percentages, in its account and overall.

        A buy adds the order's cost to the position and a sell removes it; paying for a buy moves
        cash into stock, so an account's total is unchanged and only its mix shifts. None when the
        account details are unavailable (the preview still works without them)."""
        if self.accounts_loader is None:
            return None
        try:
            accounts = self.accounts_loader().get("accounts", [])
        # the impact is an extra; a broker hiccup must not block a preview
        except Exception:  # noqa: BLE001
            return None
        price = order.price if order.price is not None else last
        cost = order.quantity * price
        sign = 1 if order.side == "buy" else -1
        mine = next(
            (
                a
                for a in accounts
                if a.get("broker") == order.broker
                and (order.broker == "binance" or a.get("id") == order.account)
            ),
            None,
        )
        if mine is None:
            return None
        symbol = order.ticker
        held_here = (mine.get("holdings") or {}).get(symbol, {})
        value_here, qty_here = (
            float(held_here.get("market_value", 0.0)),
            float(held_here.get("quantity", 0.0)),
        )
        total_here = float(mine.get("total_value") or 0.0)
        value_all = sum(
            float((a.get("holdings") or {}).get(symbol, {}).get("market_value", 0.0))
            for a in accounts
        )
        qty_all = sum(
            float((a.get("holdings") or {}).get(symbol, {}).get("quantity", 0.0)) for a in accounts
        )
        total_all = sum(float(a.get("total_value") or 0.0) for a in accounts)

        def pct(part: float, whole: float) -> float | None:
            return part / whole * 100 if whole > 0 else None

        after_here, after_all = value_here + sign * cost, value_all + sign * cost
        cash = mine.get("cash")
        power = mine.get("spendable")
        power = mine.get("buying_power") if power is None else power
        return {
            "symbol": symbol,
            "cost": cost,
            "account": {
                "label": mine.get("label"),
                "quantity": {"before": qty_here, "after": qty_here + sign * order.quantity},
                "value": {"before": value_here, "after": after_here},
                "percent": {
                    "before": pct(value_here, total_here),
                    "after": pct(after_here, total_here),
                },
                "total": total_here,
                "cash_after": None if cash is None else float(cash) - sign * cost,
                "buying_power_after": None if power is None else float(power) - sign * cost,
            },
            "holdings": self.holdings_rows(
                symbol, accounts, mine, sign * cost, sign * order.quantity
            ),
            "everything": self._with_statements(
                symbol, value_all, qty_all, total_all, cost, sign, order
            ),
            "overall": {
                "quantity": {"before": qty_all, "after": qty_all + sign * order.quantity},
                "value": {"before": value_all, "after": after_all},
                "percent": {
                    "before": pct(value_all, total_all),
                    "after": pct(after_all, total_all),
                },
                "total": total_all,
            },
        }

    @staticmethod
    def holdings_rows(
        symbol: str,
        accounts: list[dict[str, Any]],
        chosen: dict[str, Any] | None = None,
        value_change: float = 0.0,
        qty_change: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Where *symbol* is held now, account by account (and after the order, in the chosen one)."""
        rows = []
        for a in accounts:
            held = (a.get("holdings") or {}).get(symbol, {})
            qty, value = float(held.get("quantity", 0.0)), float(held.get("market_value", 0.0))
            total = float(a.get("total_value") or 0.0)
            row = {
                "label": a.get("label"),
                "id": a.get("id"),
                "broker": a.get("broker"),
                "quantity": qty,
                "value": value,
                "percent": value / total * 100 if total > 0 else None,
                "total": total,
                # what these shares cost on average, where the broker says (Schwab does)
                "average_price": held.get("average_price"),
            }
            if chosen is not None and a is chosen:
                after = value + value_change
                row.update(
                    chosen=True,
                    quantity_after=qty + qty_change,
                    value_after=after,
                    percent_after=after / total * 100 if total > 0 else None,
                )
            rows.append(row)
        return rows

    def holdings_for(self, ticker: str) -> dict[str, Any]:
        """Where a ticker is held today: every live account and any statement institution."""
        ticker = str(ticker).strip().upper()
        if not TICKER.fullmatch(ticker):
            raise TradingError("a ticker is required")
        accounts = (self.accounts_loader().get("accounts", [])) if self.accounts_loader else []
        statements = []
        if self.statements_loader:
            try:
                other = self.statements_loader()
            except Exception:  # noqa: BLE001 - an extra
                other = {}
            held = (other.get("holdings") or {}).get(ticker)
            if held:
                institutions = list(other["institutions"].values())
                statements = [
                    {
                        "name": ", ".join(i["name"] for i in institutions),
                        "as_of": max(i["as_of"] for i in institutions),
                        **held,
                    }
                ]
        return {
            "symbol": ticker,
            "accounts": self.holdings_rows(ticker, accounts),
            "statements": statements,
        }

    def _with_statements(
        self,
        symbol: str,
        value_all: float,
        qty_all: float,
        total_all: float,
        cost: float,
        sign: int,
        order: Order,
    ) -> dict[str, Any] | None:
        """The same position counting institutions that are not read live (e.g. Fidelity, from statements)."""
        if self.statements_loader is None:
            return None
        try:
            other = self.statements_loader()
        except Exception:  # noqa: BLE001 - an extra; never blocks a preview
            return None
        if not other:
            return None
        held = other.get("holdings", {}).get(symbol, {})
        value = value_all + float(held.get("market_value", 0.0))
        qty = qty_all + float(held.get("quantity", 0.0))
        total = total_all + sum(float(i.get("total", 0.0)) for i in other["institutions"].values())
        after = value + sign * cost
        return {
            "institutions": [
                {"name": i["name"], "as_of": i["as_of"], "total": i["total"]}
                for i in other["institutions"].values()
            ],
            "quantity": {"before": qty, "after": qty + sign * order.quantity},
            "value": {"before": value, "after": after},
            "percent": {
                "before": value / total * 100 if total > 0 else None,
                "after": after / total * 100 if total > 0 else None,
            },
            "total": total,
        }

    def quote(self, ticker: str) -> dict[str, Any]:
        """The current mark and the default limit prices for a buy and a sell (read-only)."""
        ticker = str(ticker).strip().upper()
        if not TICKER.fullmatch(ticker):
            raise TradingError("a ticker is required")
        if not self.policy.enabled:
            raise TradingError("trading is turned off in the policy")
        self.policy.check_ticker(ticker)
        name = "binance" if ticker.endswith("-USD") else "schwab"
        if name not in self.policy.brokers or name not in self.brokers:
            raise TradingError(f"{name} is not enabled for trading")
        broker = self.brokers[name]()
        mark = broker.last_price(ticker)
        places = 2 if mark >= 1 else 6

        def default(offset: float) -> float:
            price = round(mark * (1 + offset / 100), places)
            return broker.round_price(ticker, price) if hasattr(broker, "round_price") else price

        return {
            "ticker": ticker,
            "mark": mark,
            "buy_price": default(self.policy.buy_offset_pct),
            "sell_price": default(self.policy.sell_offset_pct),
            "templates": [
                {
                    "name": name,
                    "side": side,
                    "offset_pct": offset,
                    "price": default(offset),
                    **({"tif": tif} if tif else {}),
                    **({"session": session} if session else {}),
                }
                for name, side, offset, tif, session in (
                    DEFAULT_TEMPLATES if self.policy.templates is None else self.policy.templates
                )
            ],
        }

    def _limits(self, order: Order, last: float) -> float:
        """Check the dollar caps and price band; returns the notional."""
        notional = order.quantity * (order.price if order.price is not None else last)
        if self.policy.per_order_usd <= 0 or notional > self.policy.per_order_usd:
            raise TradingError(
                f"this order is about ${notional:,.2f}, over the ${self.policy.per_order_usd:,.2f} per-order limit"
            )
        spent = self.audit.spent_today()
        if self.policy.daily_usd <= 0 or spent + notional > self.policy.daily_usd:
            raise TradingError(
                f"with ${spent:,.2f} already sent today this would pass the ${self.policy.daily_usd:,.2f} daily limit"
            )
        if order.price is not None:
            band = self.policy.price_band_pct
            if band <= 0 or abs(order.price - last) / last * 100 > band:
                raise TradingError(
                    f"the limit price is more than {band:g}% away from the last price {last:,.4g}"
                )
        return notional

    def preview(self, raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):  # the policy's defaults apply to whatever the request leaves out
            defaults = {"session": self.policy.default_session, "tif": self.policy.default_tif}
            if (
                raw.get("kind") == "market"
            ):  # a market order is a regular-hours day order unless told
                defaults = {"session": "normal", "tif": "day"}
            raw = {**defaults, **raw}
        order = parse_order(raw)
        broker = self._broker(order)
        last = broker.last_price(order.ticker)
        if hasattr(broker, "check_order"):
            broker.check_order(order)
        notional = self._limits(order, last)
        checked = broker.preview(order)
        token = secrets.token_urlsafe(18)
        with self._lock:
            now = self.now()
            self._previews = {k: v for k, v in self._previews.items() if v[1] > now}
            if len(self._previews) >= MAX_PREVIEWS:
                raise TradingError("too many pending previews; wait a moment")
            self._previews[token] = (order, now + PREVIEW_TTL_SECONDS, notional)
        self.audit.write("preview", order=_describe(order), notional=notional, broker=order.broker)
        return {
            "impact": self.impact(order, last),
            "confirmation": self.policy.confirmation,
            "preview_id": token,
            "expires_in": PREVIEW_TTL_SECONDS,
            "phrase": order.phrase,
            "order": _describe(order),
            "broker": order.broker,
            "last_price": last,
            "notional": notional,
            "warnings": checked["warnings"],
        }

    def confirm(self, token: str, phrase: str) -> dict[str, Any]:
        with self._lock:
            entry = self._previews.pop(str(token), None)  # single use, whatever happens next
        if entry is None or entry[1] <= self.now():
            raise TradingError(
                "that preview has expired or was already used; preview the order again"
            )
        order, _, _ = entry
        if not isinstance(order, Order):
            raise TradingError("that preview is for a withdrawal, not an order")
        if self.policy.confirmation == "phrase" and str(phrase).strip() != order.phrase:
            self.audit.write("rejected", reason="phrase", order=_describe(order))
            raise TradingError(f'type exactly "{order.phrase}" to confirm')
        broker = self._broker(order)
        last = broker.last_price(order.ticker)
        notional = self._limits(order, last)
        client_id = "tradility-" + secrets.token_hex(8)
        self.audit.write(
            "sending",
            order=_describe(order),
            notional=notional,
            broker=order.broker,
            client_id=client_id,
        )
        try:
            placed = broker.place(order, client_id)
        except TradingError as exc:
            self.audit.write(
                "failed", order=_describe(order), error=str(exc)[:300], client_id=client_id
            )
            raise
        except (OSError, URLError, ValueError) as exc:
            self.audit.write(
                "unknown",
                order=_describe(order),
                notional=notional,
                client_id=client_id,
                error=type(exc).__name__,
            )
            raise TradingError(
                "the order's outcome is unknown; check your open orders before trying again"
            ) from exc
        self.audit.write(
            "placed",
            order=_describe(order),
            notional=notional,
            broker=order.broker,
            broker_order_id=placed.broker_order_id,
            client_id=client_id,
        )
        return {
            "placed": True,
            "broker": order.broker,
            "broker_order_id": placed.broker_order_id,
            "order": _describe(order),
        }

    # -- withdrawals: a separate switch, address allowlist, caps, preview and typed phrase ----------

    WITHDRAW_EVENTS = frozenset({"withdrawn", "withdraw_unknown"})

    def _withdrawer(self) -> Any:
        if not self.policy.enabled or not self.policy.withdrawals_enabled:
            raise TradingError("withdrawals are turned off in the policy")
        if "binance_withdraw" not in self.brokers:
            raise TradingError("no Binance.US withdrawal credentials are configured")
        return self.brokers["binance_withdraw"]()

    def withdraw_preview(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise TradingError("a withdrawal is an object")
        label = str(raw.get("label", "")).strip()
        entry = next((a for a in self.policy.withdraw_addresses if a[0] == label), None)
        if entry is None:
            raise TradingError("that destination is not on the policy's withdrawal allowlist")
        _, coin, network, address = entry
        try:
            amount = float(raw.get("amount"))
        except (TypeError, ValueError) as exc:
            raise TradingError("amount must be a number") from exc
        if not math.isfinite(amount) or amount <= 0:
            raise TradingError("amount must be positive")
        broker = self._withdrawer()
        notional = amount * broker.usd_price(coin)
        if self.policy.withdraw_per_usd <= 0 or notional > self.policy.withdraw_per_usd:
            raise TradingError(
                f"this withdrawal is about ${notional:,.2f}, over the ${self.policy.withdraw_per_usd:,.2f} per-withdrawal limit"
            )
        spent = self.audit.spent_today(events=self.WITHDRAW_EVENTS)
        if self.policy.withdraw_daily_usd <= 0 or spent + notional > self.policy.withdraw_daily_usd:
            raise TradingError(
                f"with ${spent:,.2f} already withdrawn today this would pass the ${self.policy.withdraw_daily_usd:,.2f} daily limit"
            )
        if broker.balance(coin) < amount:
            raise TradingError(f"not enough {coin} available to withdraw")
        phrase = f"WITHDRAW {amount:g} {coin} TO {label}"
        token = secrets.token_urlsafe(18)
        with self._lock:
            now = self.now()
            self._previews = {k: v for k, v in self._previews.items() if v[1] > now}
            self._previews[token] = (
                (label, coin, network, address, amount, phrase),
                now + PREVIEW_TTL_SECONDS,
                notional,
            )  # type: ignore[assignment]
        self.audit.write(
            "withdraw_preview", label=label, coin=coin, amount=amount, notional=notional
        )
        return {
            "preview_id": token,
            "expires_in": PREVIEW_TTL_SECONDS,
            "phrase": phrase,
            "label": label,
            "coin": coin,
            "network": network,
            "to": "…" + address[-6:],
            "amount": amount,
            "notional": notional,
        }

    def withdraw_confirm(self, token: str, phrase: str) -> dict[str, Any]:
        with self._lock:
            entry = self._previews.pop(str(token), None)
        if (
            entry is None
            or entry[1] <= self.now()
            or not isinstance(entry[0], tuple)
            or len(entry[0]) != 6
        ):
            raise TradingError(
                "that preview has expired or was already used; preview the withdrawal again"
            )
        (label, coin, network, address, amount, expected), _, notional = entry
        if str(phrase).strip() != expected:
            self.audit.write("withdraw_rejected", reason="phrase", label=label)
            raise TradingError(f'type exactly "{expected}" to confirm')
        broker = self._withdrawer()
        # the destination must still be on the allowlist, and the caps are checked again
        if (label, coin, network, address) not in self.policy.withdraw_addresses:
            raise TradingError("that destination is no longer on the withdrawal allowlist")
        notional = amount * broker.usd_price(coin)
        spent = self.audit.spent_today(events=self.WITHDRAW_EVENTS)
        if (
            self.policy.withdraw_per_usd <= 0
            or notional > self.policy.withdraw_per_usd
            or self.policy.withdraw_daily_usd <= 0
            or spent + notional > self.policy.withdraw_daily_usd
        ):
            raise TradingError("this withdrawal is over a policy limit now; preview it again")
        client_id = "tradility-w-" + secrets.token_hex(8)
        self.audit.write(
            "withdraw_sending",
            label=label,
            coin=coin,
            amount=amount,
            notional=notional,
            client_id=client_id,
        )
        try:
            withdrawal_id = broker.withdraw(coin, network, address, amount, client_id)
        except TradingError as exc:
            self.audit.write(
                "withdraw_failed", label=label, error=str(exc)[:300], client_id=client_id
            )
            raise
        except (OSError, URLError, ValueError) as exc:
            self.audit.write(
                "withdraw_unknown",
                label=label,
                coin=coin,
                amount=amount,
                notional=notional,
                client_id=client_id,
                error=type(exc).__name__,
            )
            raise TradingError(
                "the withdrawal's outcome is unknown; check Binance.US before trying again"
            ) from exc
        self.audit.write(
            "withdrawn",
            label=label,
            coin=coin,
            amount=amount,
            notional=notional,
            withdrawal_id=withdrawal_id,
            client_id=client_id,
        )
        return {
            "withdrawn": True,
            "label": label,
            "coin": coin,
            "amount": amount,
            "withdrawal_id": withdrawal_id,
        }

    def cancel(
        self, broker_name: str, order_id: str, phrase: str, symbol: str = "", account: str = ""
    ) -> dict[str, Any]:
        if str(phrase).strip() != f"CANCEL {order_id}":
            raise TradingError(f'type exactly "CANCEL {order_id}" to confirm')
        if (
            not self.policy.enabled
            or broker_name not in self.policy.brokers
            or broker_name not in self.brokers
        ):
            raise TradingError(f"{broker_name} is not enabled for trading")
        if broker_name == "schwab" and account not in self.policy.schwab_accounts:
            raise TradingError("choose one of the Schwab accounts the policy allows")
        broker = self.brokers[broker_name]()
        self.audit.write("cancelling", broker=broker_name, broker_order_id=order_id)
        try:
            if broker_name == "binance":
                broker.cancel(order_id, symbol)
            else:
                broker.cancel(order_id, account)
        except TradingError as exc:
            self.audit.write(
                "cancel_failed", broker=broker_name, broker_order_id=order_id, error=str(exc)[:300]
            )
            raise
        self.audit.write("cancelled", broker=broker_name, broker_order_id=order_id)
        return {"cancelled": True, "broker": broker_name, "broker_order_id": order_id}


def _describe(order: Order) -> dict[str, Any]:
    described: dict[str, Any] = {
        "ticker": order.ticker,
        "side": order.side,
        "kind": order.kind,
        "quantity": order.quantity,
        "price": order.price,
    }
    if order.account:
        described["account"] = order.account
    if order.broker == "schwab":
        described["session"], described["tif"] = order.session, order.tif
    return described


def build_brokers(
    policy: Policy, env: dict[str, str] | None = None, send: Send = http_send
) -> dict[str, Callable[[], Any]]:
    """Broker factories that fetch credentials only when an order needs them."""
    env = os.environ if env is None else env

    def schwab() -> SchwabTrader:
        token = resolve_access_token(
            env.get("TRADILITY_SCHWAB_ACCESS_TOKEN", ""),
            env.get("TRADILITY_SCHWAB_TOKEN_COMMAND", ""),
        )
        if token.error:
            raise TradingError(token.error)
        return SchwabTrader(token.token, send)

    def binance() -> BinanceTrader:
        command = env.get("TRADILITY_BINANCE_TRADE_CREDENTIAL_COMMAND", "").strip()
        if not command:
            raise TradingError("no Binance.US trade credentials are configured")
        creds = run_credential_command(command)
        key, secret = (
            str(creds.get("api_key", "")).strip(),
            str(creds.get("api_secret", "")).strip(),
        )
        if not key or not secret:
            raise TradingError("the Binance.US trade credential command returned no key")
        return BinanceTrader(key, secret, send)

    def binance_withdraw() -> BinanceWithdrawer:
        command = env.get("TRADILITY_BINANCE_WITHDRAW_CREDENTIAL_COMMAND", "").strip()
        if not command:
            raise TradingError("no Binance.US withdrawal credentials are configured")
        creds = run_credential_command(command)
        key, secret = (
            str(creds.get("api_key", "")).strip(),
            str(creds.get("api_secret", "")).strip(),
        )
        if not key or not secret:
            raise TradingError("the Binance.US withdrawal credential command returned no key")
        return BinanceWithdrawer(key, secret, send)

    return {"schwab": schwab, "binance": binance, "binance_withdraw": binance_withdraw}


__all__ = [
    "TradingError",
    "Policy",
    "load_policy",
    "Order",
    "parse_order",
    "Audit",
    "Trader",
    "build_brokers",
    "SchwabTrader",
    "BinanceTrader",
]
