"""One page template serves the standalone app and Clockwork's embedded /to-tradility."""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "src" / "tradility" / "templates" / "index.html"


@pytest.fixture(scope="module")
def render():
    env = jinja2.Environment(
        loader=jinja2.DictLoader(
            {"_theme_head.html": "<!--THEME-->", "_site_nav.html": "<!--SITE-NAV-->"}
        )
    )
    env.globals["url_for"] = lambda endpoint, **kw: "/static/" + kw.get("filename", "")
    template = env.from_string(TEMPLATE.read_text(encoding="utf-8"))
    return lambda **kw: template.render(**kw)


def test_standalone_is_the_default(render):
    for page in (render(), render(embedded=False)):
        assert "<title>Tradility</title>" in page
        assert 'class="header-title" href="/"' in page
        assert "Standalone web application" in page
        assert "initCollapsibleCards();" in page
        assert "<!--SITE-NAV-->" not in page
        assert "<!--THEME-->" not in page


def test_embedded_takes_clockwork_chrome_and_its_shared_card_script(render):
    page = render(embedded=True)
    assert "<title>Tradility — Clockwork</title>" in page
    assert "<!--SITE-NAV-->" in page and "<!--THEME-->" in page
    assert "Standalone web application" not in page
    # Clockwork's shared collapsible-cards.js folds the cards; the inline copy must not run too.
    assert "initCollapsibleCards" not in page
    assert 'id="detail-overlay" data-cc="on"' in page
    assert "#invest-item-" in page


def test_the_two_modes_differ_only_in_the_embedding_points(render):
    standalone = render().splitlines()
    embedded = render(embedded=True).splitlines()
    assert len(standalone) > 3000 and len(embedded) > 3000
    # the page body (cards, tabs, scripts) is shared: nearly every line is common to both
    common = set(standalone) & set(embedded)
    assert len(common) / len(set(standalone)) > 0.95
