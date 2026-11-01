"""The Fidelity trade-ticket driver: checks the preview against the order, then (maybe) submits.

The page-driving tests use a local mock of a trade ticket in a real headless browser; they are skipped
where Playwright is not installed. Nothing here ever touches Fidelity."""

from pathlib import Path

import pytest

from tradility import fidelity_browser as fb

MOCK = Path(__file__).parent / "fixtures" / "fidelity_trade_mock.html"

LIMIT = fb.Ticket("4917", "VWO", "buy", "limit", 2, 54.12, "gtc", "normal")


def page_text(**kw):
    parts = {
        "acct": "Individual TOD ***4917",
        "action": "Buy",
        "qty": "2",
        "sym": "VWO",
        "type": "Limit at $54.12",
        "tif": "Good till canceled",
        "ext": "Extended hours: No",
    }
    parts.update(kw)
    return f"Review {parts['acct']} {parts['action']} {parts['qty']} shares of {parts['sym']} {parts['type']} {parts['tif']} {parts['ext']}"


def test_a_matching_preview_is_confirmed_item_by_item():
    confirmed = fb.verify_preview(page_text(), LIMIT)
    assert "symbol VWO" in confirmed and "action buy" in confirmed and "quantity 2" in confirmed
    assert (
        "limit price 54.12" in confirmed
        and "good till canceled" in confirmed
        and "account …4917" in confirmed
    )


@pytest.mark.parametrize(
    "change",
    [
        {"sym": "VOO"},
        {"action": "Sell"},
        {"qty": "20"},
        {"qty": "3"},
        {"type": "Limit at $55.12"},
        {"type": "Market"},
        {"tif": "Day"},
        {"acct": "HSA ***9422"},
    ],
)
def test_any_difference_between_the_page_and_the_order_stops_everything(change):
    with pytest.raises(fb.Mismatch, match="nothing was submitted"):
        fb.verify_preview(page_text(**change), LIMIT)


def test_prices_are_matched_in_the_ways_a_page_writes_them_but_not_as_part_of_a_larger_number():
    for written in ("$1,054.12", "$54.1200"):
        big = fb.Ticket("4917", "VWO", "buy", "limit", 2, 1054.12, "gtc", "normal")
        if "," in written:
            fb.verify_preview(page_text(type=f"Limit at {written}"), big)
    with pytest.raises(fb.Mismatch):
        fb.verify_preview(
            page_text(type="Limit at $154.12"), LIMIT
        )  # 54.12 inside 154.12 is not a match


def test_a_market_day_order_is_checked_too():
    market = fb.Ticket("9422", "AAPL", "sell", "market", 1.5, None, "day", "normal")
    ok = "HSA ***9422 Sell 1.5 shares of AAPL Market Day Extended hours: No"
    assert "market order" in fb.verify_preview(ok, market)
    with pytest.raises(fb.Mismatch):
        fb.verify_preview(ok.replace("Market", "Limit"), market)


def test_a_ticket_is_built_from_the_service_order():
    t = fb.Ticket.from_order(
        {
            "account": "4917",
            "ticker": "VWO",
            "side": "buy",
            "kind": "limit",
            "quantity": 2,
            "price": 54.12,
            "tif": "gtc",
            "session": "normal",
        }
    )
    assert t == LIMIT


# -- the real browser against the mock page -------------------------------------------------------

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def page(monkeypatch, tmp_path):
    monkeypatch.setattr(fb, "TRADE_URL", MOCK.as_uri())
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no browser available: {type(exc).__name__}")
        context = browser.new_context(viewport={"width": 1200, "height": 900})
        yield context.new_page()
        browser.close()


def test_the_driver_fills_the_mock_ticket_previews_it_and_reads_back_the_review(page, tmp_path):
    driver = fb.FidelityDriver(page, shots=tmp_path / "shots")
    staged = driver.stage(LIMIT)
    assert "ABC" not in staged.preview_text and "***4917" in staged.preview_text
    assert "Good till canceled" in staged.preview_text and "$54.12" in staged.preview_text
    assert Path(staged.screenshot).is_file() and "limit order" in staged.checks
    assert page.evaluate("window.__placed || 0") == 0  # staging never places anything


def test_submit_presses_the_final_button_once_and_only_after_a_verified_stage(page, tmp_path):
    driver = fb.FidelityDriver(page)
    with pytest.raises(fb.FidelityError, match="nothing is staged"):
        driver.submit()
    driver.stage(LIMIT)
    done = driver.submit()
    assert done["order_id"] == "ABC123456" and page.evaluate("window.__placed") == 1
    with pytest.raises(fb.FidelityError, match="nothing is staged"):
        driver.submit()  # one submit per stage
    assert page.evaluate("window.__placed") == 1


def test_a_page_that_echoes_a_different_order_is_a_mismatch_and_nothing_is_placed(page):
    page.goto(MOCK.as_uri())
    page.evaluate("window.__mismatch = true")
    driver = fb.FidelityDriver(page)
    driver.open = lambda: None  # keep the flag set on this page load
    with pytest.raises(fb.Mismatch):
        driver.stage(LIMIT)
    assert driver.staged is None and (page.evaluate("window.__placed || 0")) == 0
    with pytest.raises(fb.FidelityError, match="nothing is staged"):
        driver.submit()


def test_market_orders_extended_hours_and_the_other_account_are_filled_correctly(page):
    ticket = fb.Ticket("9422", "VWO", "sell", "market", 1, None, "day", "extended")
    staged = fb.FidelityDriver(page).stage(ticket)
    assert (
        "HSA ***9422" in staged.preview_text
        and "Sell" in staged.preview_text
        and "Extended hours: Yes" in staged.preview_text
    )


def test_a_signed_out_browser_is_reported_not_driven(monkeypatch, tmp_path):
    login = tmp_path / "login.html"
    login.write_text("<html><body>Log in</body></html>")
    monkeypatch.setattr(fb, "TRADE_URL", login.as_uri())
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception:  # noqa: BLE001
            pytest.skip("no browser available")
        page = browser.new_page()
        with pytest.raises(fb.NeedsLogin, match="signed out"):
            fb.FidelityDriver(page).stage(LIMIT)
        browser.close()


# -- the worker protocol, the client and the trader's use of them ---------------------------------

import json  # noqa: E402
import sys  # noqa: E402

from tradility import fidelity_worker as fw  # noqa: E402
from tradility import trading as t  # noqa: E402
from tradility.fidelity_client import WorkerClient  # noqa: E402


class FakeDriver:
    def __init__(self, error=None):
        self.error, self.staged, self.submitted = error, None, 0

    def stage(self, ticket):
        if self.error:
            raise self.error
        self.staged = ticket
        return fb.Staged("Review ...", "/tmp/x.png", ["symbol VWO"])

    def submit(self):
        self.submitted += 1
        return {"order_id": "ABC123456", "confirmation": "placed"}


ORDER = {
    "account": "4917",
    "ticker": "VWO",
    "side": "buy",
    "kind": "limit",
    "quantity": 2,
    "price": 54.12,
    "tif": "gtc",
    "session": "normal",
}


def test_the_worker_turns_every_outcome_into_a_reply_and_never_an_exception():
    driver = FakeDriver()
    assert fw.handle(driver, {"cmd": "stage", "order": ORDER})["staged"]["checks"] == ["symbol VWO"]
    assert fw.handle(driver, {"cmd": "submit"})["result"]["order_id"] == "ABC123456"
    assert fw.handle(driver, {"cmd": "nope"})["ok"] is False
    for error, kind in (
        (fb.NeedsLogin("signed out"), "needs_login"),
        (fb.Mismatch("differs"), "mismatch"),
        (fb.FidelityError("gone"), "error"),
        (KeyError("secret-account-12345"), "error"),
    ):
        reply = fw.handle(FakeDriver(error), {"cmd": "stage", "order": ORDER})
        assert reply["ok"] is False and reply["kind"] == kind
    assert (
        "secret-account"
        not in fw.handle(
            FakeDriver(KeyError("secret-account-12345")), {"cmd": "stage", "order": ORDER}
        )["error"]
    )


FAKE_WORKER = (
    "import sys, json\n"
    "for line in sys.stdin:\n"
    "    r = json.loads(line)\n"
    "    if r['cmd'] == 'quit': break\n"
    "    print(json.dumps({'ok': True, 'echo': r['cmd']}), flush=True)\n"
)


def test_the_client_talks_json_lines_to_a_separate_process_and_restarts_it_if_it_dies():
    client = WorkerClient(argv=[sys.executable, "-c", FAKE_WORKER])
    assert client.stage(ORDER)["echo"] == "stage" and client.submit()["echo"] == "submit"
    client._proc.kill()
    client._proc.wait()
    assert client.stage(ORDER)["ok"] is True  # a new process was started
    client.close()
    dead = WorkerClient(argv=[sys.executable, "-c", "pass"])
    assert (
        dead.stage(ORDER)["ok"] is False
    )  # the worker exits without answering: reported, not raised
    with pytest.raises(OSError):
        dead.submit()  # but a lost submit is an unknown outcome the trader must record


POLICY = t.Policy(
    enabled=True,
    per_order_usd=1000,
    daily_usd=1500,
    price_band_pct=10,
    brokers=("schwab",),
    tickers=frozenset({"*"}),
    fidelity_accounts=("4917",),
    fidelity_automation="submit",
)


def fidelity_trader(tmp_path, automation="submit", driver=None, confirmation="button"):
    from tests.test_trading import FID_STATEMENTS, Fake

    send = Fake()
    trader = t.Trader(
        t.Policy(
            **{**POLICY.__dict__, "fidelity_automation": automation, "confirmation": confirmation}
        ),
        {"schwab": lambda: t.SchwabTrader("tok", send)},
        t.Audit(tmp_path / "audit.jsonl"),
    )
    trader.accounts_loader = lambda: {"accounts": []}
    trader.statements_loader = lambda: FID_STATEMENTS
    trader.fidelity_driver = driver if driver is not None else FakeDriver()
    return trader, send


def stage_request(**kw):
    return {
        "ticker": "AAPL",
        "account": "4917",
        "side": "buy",
        "kind": "limit",
        "quantity": 2,
        "price": 100,
        "tif": "gtc",
        "session": "normal",
        **kw,
    }


class AdaptingDriver(FakeDriver):
    def stage(self, order):
        self.staged = order
        return {
            "ok": True,
            "staged": {
                "preview_text": "Review ...",
                "screenshot": "",
                "checks": ["symbol AAPL", "quantity 2"],
            },
        }

    def submit(self):
        self.submitted += 1
        return {"ok": True, "result": {"order_id": "ABC123456"}}


def test_stage_then_submit_drives_fidelity_once_and_counts_toward_the_daily_cap(tmp_path):
    driver = AdaptingDriver()
    trader, _ = fidelity_trader(tmp_path, driver=driver)
    st = trader.fidelity_stage(stage_request())
    assert (
        st["staged"]["checks"]
        and st["automation"] == "submit"
        and driver.staged["session"] == "normal"
    )
    assert driver.submitted == 0  # staging submits nothing
    done = trader.fidelity_submit(st["preview_id"], "")
    assert done == {
        "placed": True,
        "broker": "fidelity",
        "broker_order_id": "ABC123456",
        "order": done["order"],
    }
    assert driver.submitted == 1 and trader.audit.spent_today() == 200
    with pytest.raises(t.TradingError, match="expired or was already used"):
        trader.fidelity_submit(st["preview_id"], "")
    assert driver.submitted == 1


def test_stage_mode_never_submits_and_off_never_even_stages(tmp_path):
    driver = AdaptingDriver()
    trader, _ = fidelity_trader(tmp_path, "stage", driver)
    st = trader.fidelity_stage(stage_request())
    with pytest.raises(t.TradingError, match="submitting to Fidelity is off"):
        trader.fidelity_submit(st["preview_id"], "")
    assert driver.submitted == 0
    off, _ = fidelity_trader(tmp_path / "o", "off", AdaptingDriver())
    with pytest.raises(t.TradingError, match="turned off in the policy"):
        off.fidelity_stage(stage_request())


def test_a_mismatch_or_a_signed_out_browser_stops_the_stage_and_phrase_mode_wants_the_phrase(
    tmp_path,
):
    class Failing(AdaptingDriver):
        def stage(self, order):
            return {
                "ok": False,
                "kind": "mismatch",
                "error": "Fidelity's preview does not match the order; nothing was submitted",
            }

    trader, _ = fidelity_trader(tmp_path, driver=Failing())
    with pytest.raises(t.TradingError, match="does not match"):
        trader.fidelity_stage(stage_request())
    assert not trader._previews  # nothing was left to submit
    driver = AdaptingDriver()
    phrase_trader, _ = fidelity_trader(tmp_path / "p", driver=driver, confirmation="phrase")
    st = phrase_trader.fidelity_stage(stage_request())
    assert st["phrase"] == "BUY AAPL AT FIDELITY"
    with pytest.raises(t.TradingError, match="type exactly"):
        phrase_trader.fidelity_submit(st["preview_id"], "buy")
    assert driver.submitted == 0


def test_a_lost_submit_is_unknown_counted_and_the_policy_limits_hold_at_submit_time(tmp_path):
    class Lost(AdaptingDriver):
        def submit(self):
            raise OSError("the Fidelity browser closed")

    trader, _ = fidelity_trader(tmp_path, driver=Lost())
    st = trader.fidelity_stage(stage_request())
    with pytest.raises(t.TradingError, match="outcome is unknown"):
        trader.fidelity_submit(st["preview_id"], "")
    assert trader.audit.spent_today() == 200
    capped, _ = fidelity_trader(tmp_path / "c", driver=AdaptingDriver())
    st = capped.fidelity_stage(stage_request())
    capped.policy = t.Policy(**{**capped.policy.__dict__, "daily_usd": 100})
    with pytest.raises(t.TradingError, match="daily limit"):
        capped.fidelity_submit(st["preview_id"], "")


def test_an_order_preview_cannot_be_submitted_to_fidelity_or_the_reverse(tmp_path):
    from tests.test_trading import ACCOUNTS  # noqa: F401

    trader, _ = fidelity_trader(tmp_path, driver=AdaptingDriver())
    st = trader.fidelity_stage(stage_request())
    with pytest.raises(t.TradingError, match="not this kind of order"):
        trader.confirm(st["preview_id"], "")


def test_the_service_exposes_stage_and_submit_and_reports_the_automation_mode(
    tmp_path, monkeypatch
):
    from tests.test_trading import ACCOUNTS, FID_STATEMENTS, Fake
    from tradility import trader_service as ts

    monkeypatch.setenv("TRADILITY_SCHWAB_ACCESS_TOKEN", "tok")
    policy = t.Policy(**{**POLICY.__dict__, "fidelity_automation": "stage"})
    svc = ts.Service(lambda: policy, t.Audit(tmp_path / "a.jsonl"), send=Fake())
    svc.account_loader = lambda: ACCOUNTS
    svc._statements = lambda: FID_STATEMENTS
    svc.trader.statements_loader = svc._statements
    svc.trader.accounts_loader = lambda: ACCOUNTS
    good = {"Host": "127.0.0.1:8768", "X-Tradility-Trader": "1"}
    assert svc.handle("GET", "/status", good, b"")[1]["fidelity_automation"] == "stage"
    svc._current()
    svc.trader.fidelity_driver = AdaptingDriver()
    body = json.dumps(stage_request()).encode()
    code, staged = svc.handle("POST", "/fidelity/stage", good, body)
    assert code == 200 and staged["staged"]["checks"] and staged["automation"] == "stage"
    code, refused = svc.handle(
        "POST", "/fidelity/submit", good, json.dumps({"preview_id": staged["preview_id"]}).encode()
    )
    assert code == 422 and "submitting to Fidelity is off" in refused["error"]
    assert svc.handle("POST", "/fidelity/stage", {"Host": "evil.example"}, body)[0] == 403


def test_the_policy_reads_the_fidelity_automation_mode(tmp_path):
    import os

    path = tmp_path / "p.toml"
    path.write_text('enabled = true\n[fidelity]\naccounts = ["4917"]\nautomation = "Stage"\n')
    os.chmod(path, 0o600)
    assert t.load_policy(path).fidelity_automation == "stage"
    path.write_text('[fidelity]\nautomation = "yolo"\n')
    with pytest.raises(t.TradingError, match="off, stage or submit"):
        t.load_policy(path)
    path.write_text("enabled = true\n")
    assert t.load_policy(path).fidelity_automation == "off"  # off unless asked for
