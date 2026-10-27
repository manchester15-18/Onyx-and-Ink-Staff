"""Offline tests for Emil + Impeccable Jordan tools (run as scripts, like
the other tests in this folder: tests/ is not a package, so tests insert
the repo root on sys.path themselves)."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from staff import emil_tools, impeccable_tools


class EmilTests(unittest.TestCase):
    def test_only_three_skills_wired(self):
        self.assertEqual(set(emil_tools.SKILLS), {"animate", "review-animations", "improve-animations"})

    def test_unknown_skill_rejected(self):
        out = emil_tools.get_emil_skill("/app", "animate-expo")
        self.assertIn("deliberately not wired", out)

    def test_missing_dir_message(self):
        with patch.object(emil_tools, "find_emil_dir", return_value=None):
            out = emil_tools.get_emil_skill("/app", "animate")
        self.assertIn("not found", out)

    def test_skill_text_and_brand_guard(self, tmp=None):
        # Use the real PC-mounted path if present, else a temp dir.
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "animate-SKILL.md").write_text("CSS transition first. transform and opacity only.")
            with patch.object(emil_tools, "find_emil_dir", return_value=root):
                out = emil_tools.get_emil_skill("/app", "animate")
        self.assertIn("CSS transition first", out)
        self.assertIn("BRAND + VANILLA GUARD", out)
        self.assertIn("never add framer-motion", out)


class ImpeccableTests(unittest.TestCase):
    def test_url_target_rejected(self):
        _, err = impeccable_tools._resolve_target("/app", "https://preview.onyx-and-ink-site.pages.dev")
        self.assertIn("not allowed", err)

    def test_escape_rejected(self):
        _, err = impeccable_tools._resolve_target("/app", "../../etc/passwd")
        self.assertIn("outside", err or "does not exist")

    def test_detector_missing_message(self):
        with patch.object(impeccable_tools, "find_detector", return_value=None):
            out = impeccable_tools.detect_impeccable("/app", "index.html")
        self.assertIn("not found", out)

    def test_summary_parsing(self):
        fake = '[{"antipattern": "cream-palette", "file": "index.html", "snippet": "cream bg"}]'
        class Proc:
            returncode = 2
            stdout = fake
            stderr = ""
        with patch.object(impeccable_tools, "find_detector", return_value="/usr/bin/impeccable"), \
             patch.object(impeccable_tools, "_resolve_target", return_value=(Path("/app/storefront/index.html"), None)), \
             patch("subprocess.run", return_value=Proc()):
            out = impeccable_tools.detect_impeccable("/app", "index.html")
        self.assertIn("total=1", out)
        self.assertIn("cream-palette=1", out)
        self.assertIn("exit=2", out)
        # No live mode, no edit, no shell in the command construction path.
        self.assertNotIn("live", out.split("IMPECCABLE DETECT")[0])


if __name__ == "__main__":
    unittest.main()
