"""A long-running helper that holds the Fidelity browser between staging and submitting an order.

The trader service starts it with the system Python (the one that has Playwright) and talks to it in JSON
lines on stdin and stdout, so the service itself needs no browser library. One request per line:

    {"cmd": "stage", "order": {...}}   fill the ticket, press Preview, verify it   -> {"ok": true, "staged": {...}}
    {"cmd": "submit"}                  press the final button on the staged page   -> {"ok": true, "result": {...}}
    {"cmd": "quit"}

A failure is {"ok": false, "kind": "needs_login" | "mismatch" | "error", "error": "..."} with a message that is
safe to show. The worker exits on its own after ``IDLE_SECONDS`` without a request, closing the browser.
"""

from __future__ import annotations

import json
import select
import sys
from pathlib import Path
from typing import Any

from tradility import fidelity_browser as fb

IDLE_SECONDS = 600


def labels_override() -> dict[str, str]:
    import os

    from tradility._compat import tomllib

    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    path = Path(
        os.environ.get("TRADILITY_FIDELITY_LABELS", base / "tradility" / "fidelity-labels.toml")
    )
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {k: str(v) for k, v in raw.items() if k in fb.DEFAULT_LABELS}


def handle(driver: fb.FidelityDriver, request: dict[str, Any]) -> dict[str, Any]:
    """One request against an open driver; every failure becomes a reply, never an exception."""
    try:
        if request.get("cmd") == "stage":
            staged = driver.stage(fb.Ticket.from_order(request["order"]))
            return {
                "ok": True,
                "staged": {
                    "preview_text": staged.preview_text,
                    "screenshot": staged.screenshot,
                    "checks": staged.checks,
                },
            }
        if request.get("cmd") == "submit":
            return {"ok": True, "result": driver.submit()}
        return {"ok": False, "kind": "error", "error": "unknown command"}
    except fb.NeedsLogin as exc:
        return {"ok": False, "kind": "needs_login", "error": str(exc)}
    except fb.Mismatch as exc:
        return {"ok": False, "kind": "mismatch", "error": str(exc)}
    except fb.FidelityError as exc:
        return {"ok": False, "kind": "error", "error": str(exc)}
    # class name only: a page error could echo account details
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "kind": "error", "error": f"browser error ({type(exc).__name__})"}


def main() -> int:  # pragma: no cover - needs a real browser and a signed-in profile
    from playwright.sync_api import sync_playwright

    headless = "--headed" not in sys.argv
    shots = fb.profile_dir().parent / "fidelity-shots"
    with sync_playwright() as p:
        context = fb.launch(p, headless=headless)
        page = context.pages[0] if context.pages else context.new_page()
        driver = fb.FidelityDriver(page, labels_override(), shots)
        while True:
            ready, _, _ = select.select([sys.stdin], [], [], IDLE_SECONDS)
            if not ready:
                break
            line = sys.stdin.readline()
            if not line:
                break
            try:
                request = json.loads(line)
            except ValueError:
                continue
            if request.get("cmd") == "quit":
                break
            sys.stdout.write(json.dumps(handle(driver, request)) + "\n")
            sys.stdout.flush()
        context.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
