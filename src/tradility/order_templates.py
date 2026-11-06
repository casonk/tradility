"""Saved order templates (ladders) for Tradility's standalone web application.

A template is data only: a side, hours, duration and a list of rungs (a quantity and how far from
the live price). Placing one always goes through the trader service, which expands it from its own
live price and applies the trading policy, so nothing stored here can widen what may be traded.
"""

from __future__ import annotations

import json
import math
import os
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Any

SIDES = ("buy", "sell")
TIFS = ("day", "gtc")
SESSIONS = ("normal", "extended")
MAX_TEMPLATES = 50
MAX_RUNGS = 20
MAX_NAME = 40
MAX_QUANTITY = 1_000_000_000
MAX_OFFSET_PCT = 50
LOCK = threading.Lock()


def load(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    templates = raw.get("templates") if isinstance(raw, dict) else None
    return [t for t in templates if isinstance(t, dict)] if isinstance(templates, list) else []


def save(path: Path, templates: list[dict[str, Any]]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".order-templates-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "templates": templates}, handle, indent=2, sort_keys=True)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        os.unlink(temporary)
        raise


def _finite(value: Any, what: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} must be a number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{what} must be a number")
    return number


def clean(raw: Any) -> dict[str, Any]:
    """A validated template without an id; raises ValueError saying what is wrong."""
    if not isinstance(raw, dict):
        raise ValueError("a template is an object")
    name = " ".join(str(raw.get("name", "")).split())
    if not name or len(name) > MAX_NAME:
        raise ValueError(f"name is required and at most {MAX_NAME} characters")
    side = raw.get("side")
    if side not in SIDES:
        raise ValueError("side must be buy or sell")
    tif = str(raw.get("tif", "gtc")).lower()
    session = str(raw.get("session", "normal")).lower()
    if tif not in TIFS:
        raise ValueError("duration must be day or gtc")
    if session not in SESSIONS:
        raise ValueError("hours must be normal or extended")
    rungs = raw.get("rungs")
    if not isinstance(rungs, list) or not rungs:
        raise ValueError("a template needs at least one rung")
    if len(rungs) > MAX_RUNGS:
        raise ValueError(f"a template has at most {MAX_RUNGS} rungs")
    cleaned = []
    for i, rung in enumerate(rungs, 1):
        if not isinstance(rung, dict):
            raise ValueError(f"rung {i} is an object")
        try:
            quantity = _finite(rung.get("quantity"), "quantity")
            offset = _finite(rung.get("offset_pct"), "offset_pct")
            if not 0 < quantity <= MAX_QUANTITY:
                raise ValueError("quantity must be positive")
            if abs(offset) > MAX_OFFSET_PCT:
                raise ValueError(f"offset_pct must be within {MAX_OFFSET_PCT}%")
            if (side == "buy" and offset > 0) or (side == "sell" and offset < 0):
                raise ValueError(
                    "a buy rung sits at or below the price and a sell rung at or above it"
                )
        except ValueError as exc:
            raise ValueError(f"rung {i}: {exc}") from exc
        cleaned.append({"quantity": quantity, "offset_pct": offset})
    return {"name": name, "side": side, "tif": tif, "session": session, "rungs": cleaned}


def _taken(templates: list[dict[str, Any]], name: str, except_id: str = "") -> bool:
    return any(
        t.get("name", "").casefold() == name.casefold() and t.get("id") != except_id
        for t in templates
    )


def add(templates: list[dict[str, Any]], raw: Any) -> dict[str, Any]:
    template = clean(raw)
    if len(templates) >= MAX_TEMPLATES:
        raise ValueError(f"at most {MAX_TEMPLATES} templates")
    if _taken(templates, template["name"]):
        raise ValueError("a template with that name already exists")
    template["id"] = secrets.token_hex(6)
    templates.append(template)
    return template


def update(templates: list[dict[str, Any]], template_id: str, raw: Any) -> dict[str, Any] | None:
    for index, existing in enumerate(templates):
        if existing.get("id") == template_id:
            template = clean(raw)
            if _taken(templates, template["name"], template_id):
                raise ValueError("a template with that name already exists")
            template["id"] = template_id
            templates[index] = template
            return template
    return None


def delete(templates: list[dict[str, Any]], template_id: str) -> bool:
    before = len(templates)
    templates[:] = [t for t in templates if t.get("id") != template_id]
    return len(templates) < before
