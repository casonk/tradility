"""Fill, check and (only if allowed) submit an order on Fidelity's own website.

Fidelity has no order API for individuals, and its Active Trader Pro is a Windows desktop program, so
the one way to place an order from here is to drive the website's trade ticket in a real browser.
That is unofficial: it may breach Fidelity's terms, it depends on the page's layout, and it is the
riskiest thing in this tool, so it is built to be conservative:

* The browser uses its own persistent profile. You sign in yourself, once, in a visible window
  (``python -m tradility.fidelity_browser login``), including Fidelity's two-factor step, and
  Fidelity then trusts that profile. This code never sees or types a password or a one-time code.
  When the session has lapsed it stops with ``NeedsLogin`` and asks you to sign in again.
* ``stage`` fills the ticket and presses Fidelity's own **Preview**, then reads the preview page
  back and checks that the symbol, action, quantity, price, order type and duration on it are
  exactly what was asked for (``verify_preview``). Any difference raises ``Mismatch`` and nothing
  is submitted. The page is left open, with a screenshot saved.
* ``submit`` only presses the final button on a page that ``stage`` verified, and only when the
  policy allows it (``[fidelity] automation = "submit"``).

The driver works on a Playwright page handed to it, so it is tested against a local mock of a trade
ticket and never against Fidelity.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TRADE_URL = "https://digital.fidelity.com/ftgw/digital/trade-equity/index/orderEntry"
LOGIN_HINTS = ("login", "signin", "sign-in", "authenticate")
PROFILE_ENV = "TRADILITY_FIDELITY_PROFILE"

# What each field's control is called on the page. These are regular expressions matched against the
# control's accessible name (its label or button text), so a changed id does not break them. Override
# any of them in ~/.config/tradility/fidelity-labels.toml if Fidelity renames a control.
DEFAULT_LABELS = {
    "account": r"account",
    "symbol": r"symbol",
    "quantity": r"^\s*(quantity|shares)",
    "limit_price": r"limit\s*price",
    "extended": r"extended\s*hours",
    "preview": r"preview",
    "place": r"place\s*order|submit",
}


class FidelityError(Exception):
    """A failure whose message is safe to show."""


class NeedsLogin(FidelityError):
    """The browser profile is not signed in to Fidelity."""


class Mismatch(FidelityError):
    """Fidelity's preview does not show the order that was asked for."""


@dataclass
class Ticket:
    """The order as the driver needs it (from ``Trader.fidelity_ticket``)."""

    account: str
    symbol: str
    side: str  # buy | sell
    kind: str  # limit | market
    quantity: float
    price: float | None
    tif: str  # day | gtc
    session: str  # normal | extended

    @classmethod
    def from_order(cls, order: dict[str, Any]) -> Ticket:
        return cls(
            account=str(order["account"]),
            symbol=str(order["ticker"]),
            side=str(order["side"]),
            kind=str(order["kind"]),
            quantity=float(order["quantity"]),
            price=None if order.get("price") is None else float(order["price"]),
            tif=str(order["tif"]),
            session=str(order["session"]),
        )


@dataclass
class Staged:
    """What a successful ``stage`` leaves behind."""

    preview_text: str
    screenshot: str = ""
    checks: list[str] = field(default_factory=list)


def _money(value: float) -> list[str]:
    """The ways a price may be written on the page: 55.1, 55.10, $55.10, 1,055.10."""
    return sorted(
        {f"{value:,.2f}", f"{value:.2f}", f"{value:g}", f"{value:,.4f}".rstrip("0").rstrip(".")}
    )


def verify_preview(text: str, ticket: Ticket) -> list[str]:
    """Check Fidelity's preview text against the ticket. Returns what was confirmed; raises Mismatch.

    Every item must be found; a missing one means the page shows something other than the order we
    meant to place, and submitting would be a guess."""
    flat = " ".join(text.split())
    low = flat.lower()
    confirmed: list[str] = []
    problems: list[str] = []

    def need(label: str, ok: bool) -> None:
        (confirmed if ok else problems).append(label)

    need(
        f"symbol {ticket.symbol}",
        re.search(rf"\b{re.escape(ticket.symbol)}\b", flat, re.I) is not None,
    )
    need(f"action {ticket.side}", re.search(rf"\b{ticket.side}\b", low) is not None)
    opposite = "sell" if ticket.side == "buy" else "buy"
    if re.search(rf"\b{opposite}\b", low) and not re.search(rf"\b{ticket.side}\b", low):
        problems.append(f"page says {opposite}")
    qty = f"{ticket.quantity:g}"
    need(
        f"quantity {qty}",
        re.search(rf"(?<![\d.]){re.escape(qty)}(?![\d])(?:\.0+)?(?![\d.]*\d)", flat) is not None,
    )
    if ticket.kind == "limit" and ticket.price is not None:
        need(
            f"limit price {ticket.price:.2f}",
            any(
                re.search(rf"(?<![\d.,]){re.escape(m)}(?![\d])", flat) for m in _money(ticket.price)
            ),
        )
        need("limit order", "limit" in low)
    else:
        need("market order", "market" in low)
    if ticket.tif == "gtc":
        need(
            "good till canceled",
            bool(re.search(r"good[\s-]*(?:til|till)[\s-]*cancel|\bgtc\b", low)),
        )
    else:
        need("day order", bool(re.search(r"\bday\b", low)))
    need(f"account …{ticket.account.split(':')[0]}", ticket.account.split(":")[0] in flat)
    if problems:
        raise Mismatch(
            "Fidelity's preview does not match the order ("
            + "; ".join(problems)
            + "); nothing was submitted"
        )
    return confirmed


class FidelityDriver:
    """Drives one Fidelity trade ticket on an open Playwright page."""

    def __init__(
        self, page: Any, labels: dict[str, str] | None = None, shots: Path | None = None
    ) -> None:
        self.page = page
        self.labels = {**DEFAULT_LABELS, **(labels or {})}
        self.shots = shots
        self.staged: Ticket | None = None

    # -- helpers ---------------------------------------------------------------------------------

    def _rx(self, key: str) -> re.Pattern[str]:
        return re.compile(self.labels[key], re.I)

    def _signed_out(self) -> bool:
        url = str(getattr(self.page, "url", "")).lower()
        return any(hint in url for hint in LOGIN_HINTS)

    def _first(self, *locators: Any) -> Any:
        """The first locator that matches something on the page."""
        for locator in locators:
            try:
                if locator.count() > 0:
                    return locator.first
            except Exception:  # noqa: BLE001 - a locator that cannot resolve just is not it
                continue
        raise FidelityError(
            "a control on Fidelity's trade page was not found (the page may have changed)"
        )

    def _choose(self, name: str) -> None:
        """Pick a radio, tab or button by its visible name (Buy, Limit, Day, ...)."""
        rx = re.compile(rf"^\s*{re.escape(name)}\s*$", re.I)
        control = self._first(
            self.page.get_by_role("radio", name=rx),
            self.page.get_by_role("tab", name=rx),
            self.page.get_by_role("button", name=rx),
            self.page.get_by_label(rx),
        )
        control.click()

    # -- steps -----------------------------------------------------------------------------------

    def open(self) -> None:
        self.page.goto(TRADE_URL)
        self.page.wait_for_load_state("domcontentloaded")
        if self._signed_out():
            raise NeedsLogin(
                "Fidelity is signed out: run `python -m tradility.fidelity_browser login`"
            )

    def stage(self, ticket: Ticket) -> Staged:
        """Fill the ticket, press Fidelity's Preview and check what it shows."""
        self.staged = None
        self.open()
        page = self.page
        mask = ticket.account.split(":")[0]
        account = self._first(
            page.get_by_role("combobox", name=self._rx("account")),
            page.get_by_role("button", name=self._rx("account")),
        )
        account.click()
        self._first(
            page.get_by_role("option", name=re.compile(re.escape(mask))),
            page.get_by_text(re.compile(re.escape(mask))),
        ).click()
        symbol = self._first(
            page.get_by_label(self._rx("symbol")),
            page.get_by_role("textbox", name=self._rx("symbol")),
        )
        symbol.fill(ticket.symbol)
        symbol.press("Enter")
        self._choose("Buy" if ticket.side == "buy" else "Sell")
        self._first(page.get_by_label(self._rx("quantity"))).fill(f"{ticket.quantity:g}")
        self._choose("Limit" if ticket.kind == "limit" else "Market")
        if ticket.kind == "limit" and ticket.price is not None:
            self._first(page.get_by_label(self._rx("limit_price"))).fill(f"{ticket.price:.2f}")
        self._choose("Good till canceled" if ticket.tif == "gtc" else "Day")
        extended = page.get_by_role("checkbox", name=self._rx("extended"))
        if extended.count() > 0:
            if ticket.session == "extended":
                extended.first.check()
            else:
                extended.first.uncheck()
        elif ticket.session == "extended":
            raise FidelityError("Fidelity's ticket has no extended-hours choice for this order")
        self._first(page.get_by_role("button", name=self._rx("preview"))).click()
        page.wait_for_load_state("domcontentloaded")
        text = page.inner_text("body")
        if self._signed_out():
            raise NeedsLogin("Fidelity signed the browser out; run the login step again")
        checks = verify_preview(text, ticket)  # raises Mismatch
        shot = ""
        if self.shots is not None:
            self.shots.mkdir(parents=True, exist_ok=True, mode=0o700)
            shot = str(self.shots / f"fidelity-{ticket.symbol}-{ticket.side}.png")
            page.screenshot(path=shot)
        self.staged = ticket
        return Staged(preview_text=" ".join(text.split())[:1500], screenshot=shot, checks=checks)

    def submit(self) -> dict[str, str]:
        """Press the final button on the page ``stage`` verified, and read the confirmation."""
        if self.staged is None:
            raise FidelityError("nothing is staged: stage the order first")
        ticket, self.staged = self.staged, None  # one submit per stage, whatever happens
        text = self.page.inner_text("body")
        verify_preview(text, ticket)  # still the same order on the page
        self._first(self.page.get_by_role("button", name=self._rx("place"))).click()
        self.page.wait_for_load_state("domcontentloaded")
        after = " ".join(self.page.inner_text("body").split())
        order_id = re.search(
            r"(?:order|confirmation)\s*(?:number|#|no\.?)?\s*[:#]?\s*([A-Z0-9-]{6,})", after, re.I
        )
        done = re.search(r"received|placed|submitted|accepted|thank you", after, re.I) is not None
        if not done and not order_id:
            raise FidelityError(
                "the order's outcome is unknown; check Fidelity before trying again"
            )
        return {"order_id": order_id.group(1) if order_id else "", "confirmation": after[:300]}


# -- the browser itself (not used by tests) --------------------------------------------------------


def profile_dir() -> Path:
    import os

    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return Path(os.environ.get(PROFILE_ENV, base / "tradility" / "fidelity-profile"))


def launch(playwright: Any, headless: bool) -> Any:
    """A persistent browser context on the saved profile (created owner-only)."""
    path = profile_dir()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return playwright.chromium.launch_persistent_context(
        str(path), headless=headless, viewport={"width": 1400, "height": 1000}
    )


def login() -> int:  # pragma: no cover - interactive
    """Open a visible browser on the profile so you can sign in (and pass two-factor) yourself."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = launch(p, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(TRADE_URL)
        print("Sign in to Fidelity in the window that opened, including any two-factor step and")
        print("'remember this device'. When you can see the trade ticket, close the window.")
        with contextlib.suppress(Exception):  # closing the window ends the wait
            page.wait_for_event("close", timeout=600_000)
        context.close()
    return 0


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI
    import sys

    args = argv if argv is not None else sys.argv[1:]
    if args[:1] == ["login"]:
        return login()
    print("usage: python -m tradility.fidelity_browser login", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
