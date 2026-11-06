"""The order ticket offers every saved template, and ladders preview in the pane (static page checks)."""

import re
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "src" / "tradility" / "templates" / "index.html"


class PaneTemplatesTest(unittest.TestCase):
    def setUp(self):
        self.html = PAGE.read_text()

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

    def test_inline_script_parses(self):
        script = re.findall(r"<script>(.*?)</script>", self.html, re.S)[0]
        path = Path(self._testMethodName + ".js")
        try:
            path.write_text(script)
            result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
        except FileNotFoundError:
            self.skipTest("node is not installed")
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
