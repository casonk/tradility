"""The order ticket offers every saved template, and ladders preview in the pane (static page checks)."""

import re
import subprocess
import unittest
from pathlib import Path

import jinja2

PAGE = Path(__file__).resolve().parents[1] / "src" / "tradility" / "templates" / "index.html"


def render_page(**context) -> str:
    """The page as served: the template has an ``embedded`` switch for Clockwork's copy."""
    env = jinja2.Environment(
        loader=jinja2.DictLoader({"_theme_head.html": "", "_site_nav.html": ""})
    )
    env.globals["url_for"] = lambda endpoint, **kw: "/static/" + kw.get("filename", "")
    return env.from_string(PAGE.read_text()).render(**context)


class PaneTemplatesTest(unittest.TestCase):
    def setUp(self):
        self.html = render_page()

    def test_ticket_has_a_ladder_container_and_template_loading(self):
        for needle in (
            'id="trade-ladder"',
            "ensureTemplates().then(paintTemplates)",
            "startPaneLadder",
            "paneItems",
        ):
            self.assertIn(needle, self.html)

    def test_tab_and_pane_share_one_preview_and_confirm(self):
        self.assertIn("function renderPreview(view = LADDER_TAB)", self.html)
        self.assertIn("function confirmLadder(view = LADDER_TAB)", self.html)
        self.assertEqual(self.html.count("async function confirmLadder"), 1)

    def test_every_card_collapses_from_its_title(self):
        for needle in (
            "initCollapsibleCards();",
            ".cc-collapsed",
            "tradility.cardsClosed",
            "'.detail-card, .tpl-card'",
        ):
            self.assertIn(needle, self.html)

    def test_inline_script_parses(self):
        for embedded in (False, True):
            html = render_page(embedded=embedded)
            script = re.findall(r"<script>(.*?)</script>", html, re.S)[0]
            path = Path(self._testMethodName + ".js")
            try:
                path.write_text(script)
                result = subprocess.run(
                    ["node", "--check", str(path)], capture_output=True, text=True
                )
            except FileNotFoundError:
                self.skipTest("node is not installed")
            finally:
                path.unlink(missing_ok=True)
            self.assertEqual(result.returncode, 0, f"embedded={embedded}: {result.stderr}")


if __name__ == "__main__":
    unittest.main()
