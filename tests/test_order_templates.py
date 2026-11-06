"""Saved order templates: validation, storage and the web routes (never the trader)."""

import json
import stat

import pytest

from tests.test_web import csrf, web_client  # noqa: F401
from tradility import order_templates as ot


def ladder(**kw):
    return {
        "name": "Dip ladder",
        "side": "buy",
        "tif": "gtc",
        "session": "normal",
        "rungs": [
            {"quantity": 1, "offset_pct": -1},
            {"quantity": 2, "offset_pct": -2},
            {"quantity": 3, "offset_pct": -3},
        ],
        **kw,
    }


def test_a_template_is_validated_and_cleaned():
    cleaned = ot.clean(ladder(name="  Dip   ladder  "))
    assert cleaned["name"] == "Dip ladder" and cleaned["rungs"][1] == {
        "quantity": 2.0,
        "offset_pct": -2.0,
    }
    assert cleaned["tif"] == "gtc" and cleaned["session"] == "normal"
    assert ot.clean(ladder(side="sell", rungs=[{"quantity": 1, "offset_pct": 2}]))["side"] == "sell"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"name": ""}, "name is required"),
        ({"name": "x" * 41}, "at most 40 characters"),
        ({"side": "hold"}, "side must be buy, sell or both"),
        ({"tif": "forever"}, "duration must be day or gtc"),
        ({"session": "night"}, "hours must be normal or extended"),
        ({"rungs": []}, "at least one rung"),
        ({"rungs": [{"quantity": 1, "offset_pct": -1}] * 21}, "at most 20 rungs"),
        ({"rungs": [{"quantity": 0, "offset_pct": -1}]}, "rung 1: quantity must be positive"),
        ({"rungs": [{"quantity": "x", "offset_pct": -1}]}, "rung 1: quantity must be a number"),
        (
            {"rungs": [{"quantity": 1, "offset_pct": float("nan")}]},
            "rung 1: offset_pct must be a number",
        ),
        ({"rungs": [{"quantity": 1, "offset_pct": -60}]}, "within 50%"),
        ({"rungs": [{"quantity": 1, "offset_pct": 1}]}, "rung 1: a buy rung sits at or below"),
        ({"side": "sell", "rungs": [{"quantity": 1, "offset_pct": -1}]}, "a sell rung at or above"),
    ],
)
def test_bad_templates_are_refused_with_the_reason(change, message):
    with pytest.raises(ValueError, match=message):
        ot.clean(ladder(**change))


def test_storage_is_owner_only_and_names_are_unique(tmp_path):
    path = tmp_path / "state" / "order-templates.json"
    templates = ot.load(path)
    first = ot.add(templates, ladder())
    ot.save(path, templates)
    assert (
        stat.S_IMODE(path.stat().st_mode) == 0o600
        and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    )
    assert [t["name"] for t in ot.load(path)] == ["Dip ladder"] and first["id"]
    with pytest.raises(ValueError, match="already exists"):
        ot.add(templates, ladder(name="dip LADDER"))
    second = ot.add(templates, ladder(name="Wide"))
    assert ot.update(templates, second["id"], ladder(name="Wider"))["name"] == "Wider"
    with pytest.raises(ValueError, match="already exists"):
        ot.update(templates, second["id"], ladder(name="Dip ladder"))
    assert ot.update(templates, "nope", ladder()) is None
    assert ot.delete(templates, first["id"]) is True and ot.delete(templates, first["id"]) is False
    assert ot.load(tmp_path / "missing.json") == []
    (tmp_path / "bad.json").write_text("{not json")
    assert ot.load(tmp_path / "bad.json") == []


def test_the_template_count_is_capped():
    templates = []
    for i in range(ot.MAX_TEMPLATES):
        ot.add(templates, ladder(name=f"t{i}"))
    with pytest.raises(ValueError, match="at most 50 templates"):
        ot.add(templates, ladder(name="one too many"))


def test_the_routes_create_list_update_and_delete_templates(web_client):  # noqa: F811
    client, tmp = web_client
    headers = {"X-CSRF-Token": csrf(client)}
    assert client.get("/api/tradility-templates").get_json() == {"ok": True, "templates": []}
    created = client.post("/api/tradility-templates", json=ladder(), headers=headers)
    assert created.status_code == 201
    template = created.get_json()["template"]
    assert client.get("/api/tradility-templates").get_json()["templates"] == [template]
    changed = client.put(
        f"/api/tradility-templates/{template['id']}", json=ladder(name="Renamed"), headers=headers
    )
    assert changed.status_code == 200 and changed.get_json()["template"]["name"] == "Renamed"
    assert (
        client.put("/api/tradility-templates/missing", json=ladder(), headers=headers).status_code
        == 404
    )
    assert (
        client.delete(f"/api/tradility-templates/{template['id']}", headers=headers).status_code
        == 200
    )
    assert (
        client.delete(f"/api/tradility-templates/{template['id']}", headers=headers).status_code
        == 404
    )
    assert json.loads((tmp / "order-templates.json").read_text())["templates"] == []


def test_the_routes_refuse_bad_templates_and_need_the_csrf_token(web_client):  # noqa: F811
    client, _ = web_client
    assert client.post("/api/tradility-templates", json=ladder()).status_code == 403
    assert client.delete("/api/tradility-templates/x").status_code == 403
    headers = {"X-CSRF-Token": csrf(client)}
    bad = client.post("/api/tradility-templates", json=ladder(rungs=[]), headers=headers)
    assert bad.status_code == 400 and "at least one rung" in bad.get_json()["error"]
    assert client.post("/api/tradility-templates", data="x", headers=headers).status_code == 400
    assert client.get("/api/tradility-templates").get_json()["templates"] == []


def test_the_page_has_the_templates_tab_the_builder_and_the_ladder_flow(web_client):  # noqa: F811
    client, _ = web_client
    page = client.get("/").get_data(as_text=True)
    for hook in (
        'id="tab-templates"',
        'id="templates-view"',
        "function showView",
        "function previewLadder",
        "function confirmLadder",
        "/api/tradility-trade/batch/preview",
        "/api/tradility-trade/batch/confirm",
        "/api/tradility-templates",
        "function openLadder",
    ):
        assert hook in page, hook
    # the confirmation is typed, and a failed ladder says what was and was not sent
    assert "Type <strong>${esc(p.phrase)}</strong> to confirm" in page
    assert "not sent" in page and "Stopped after" in page


def test_the_tab_can_import_the_order_forms_one_click_templates(web_client):  # noqa: F811
    client, _ = web_client
    page = client.get("/").get_data(as_text=True)
    for hook in (
        "function importPolicyTemplates",
        "const tplPolicyMissing",
        'id="tpl-import"',
        "tradeStatus.templates",
    ):
        assert hook in page, hook


def straddle(**kw):
    rungs = [
        {"side": "buy", "quantity": 1, "offset_pct": -1},
        {"side": "buy", "quantity": 2, "offset_pct": -2},
        {"side": "sell", "quantity": 1, "offset_pct": 1},
    ]
    return {**ladder(side="both"), "rungs": rungs, **kw}


def test_a_two_sided_template_keeps_each_rungs_side():
    cleaned = ot.clean(straddle())
    assert cleaned["side"] == "both"
    assert [(r["side"], r["offset_pct"]) for r in cleaned["rungs"]] == [
        ("buy", -1.0),
        ("buy", -2.0),
        ("sell", 1.0),
    ]
    # a single-side template stores no per-rung side, and accepts one that agrees with it
    one = ot.clean(ladder(rungs=[{"side": "buy", "quantity": 1, "offset_pct": -1}]))
    assert "side" not in one["rungs"][0]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            {
                "rungs": [
                    {"quantity": 1, "offset_pct": -1},
                    {"side": "sell", "quantity": 1, "offset_pct": 1},
                ]
            },
            "rung 1: a rung in a two-sided template needs a side",
        ),
        (
            {
                "rungs": [
                    {"side": "buy", "quantity": 1, "offset_pct": 1},
                    {"side": "sell", "quantity": 1, "offset_pct": 1},
                ]
            },
            "rung 1: a buy rung sits at or below",
        ),
        (
            {
                "rungs": [
                    {"side": "buy", "quantity": 1, "offset_pct": -1},
                    {"side": "sell", "quantity": 1, "offset_pct": -1},
                ]
            },
            "rung 2: a buy rung sits at or below",
        ),
        (
            {"rungs": [{"side": "buy", "quantity": 1, "offset_pct": -1}]},
            "at least one buy rung and one sell rung",
        ),
        (
            {"rungs": [{"side": "sell", "quantity": 1, "offset_pct": 1}]},
            "at least one buy rung and one sell rung",
        ),
    ],
)
def test_bad_two_sided_templates_are_refused_with_the_reason(change, message):
    with pytest.raises(ValueError, match=message):
        ot.clean(straddle(**change))


def test_a_single_side_template_refuses_a_rung_of_the_other_side():
    with pytest.raises(ValueError, match="every rung of a buy template is a buy"):
        ot.clean(ladder(rungs=[{"side": "sell", "quantity": 1, "offset_pct": 1}]))


def test_two_sided_templates_save_and_load_through_the_routes(web_client):  # noqa: F811
    client, _ = web_client
    headers = {"X-CSRF-Token": csrf(client)}
    created = client.post("/api/tradility-templates", json=straddle(), headers=headers)
    assert created.status_code == 201 and created.get_json()["template"]["side"] == "both"
    stored = client.get("/api/tradility-templates").get_json()["templates"][0]
    assert [r["side"] for r in stored["rungs"]] == ["buy", "buy", "sell"]


def test_the_tab_offers_two_sided_ladders_with_a_side_per_rung(web_client):  # noqa: F811
    client, _ = web_client
    page = client.get("/").get_data(as_text=True)
    for hook in (
        "buy + sell (straddle)",
        "const rungSide",
        "data-rside",
        "notional_by_side",
        "tpl-side-sell",
    ):
        assert hook in page, hook
