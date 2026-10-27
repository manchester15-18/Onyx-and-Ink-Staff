"""Offline tests for staff/uiux_tools.py — no network, no real skill needed."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from staff.uiux_tools import (  # noqa: E402
    BRAND_NOTE,
    MOTION_PATTERNS,
    get_motion_pattern,
    search_uiux,
)


class TestUiuxTools(unittest.TestCase):
    def test_domain_allowlist(self):
        out = search_uiux("/tmp", "gift shop", domain="not-a-domain")
        self.assertIn("unknown domain", out)

    def test_stack_allowlist(self):
        out = search_uiux("/tmp", "gift shop", stack="not-a-stack")
        self.assertIn("unknown stack", out)

    def test_query_length(self):
        out = search_uiux("/tmp", "x" * 301)
        self.assertIn("must be 1-300", out)

    def test_missing_skill(self):
        with patch("staff.uiux_tools.find_skill_dir", return_value=None):
            out = search_uiux("/tmp/definitely-not-a-project", "gift shop")
        self.assertIn("not found", out)

    def test_search_builds_safe_command(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "STYLE: minimal\n"
        fake.stderr = ""
        with patch("staff.uiux_tools.find_skill_dir", return_value=Path("/skill")), \
             patch("staff.uiux_tools.subprocess.run", return_value=fake) as run:
            out = search_uiux("/tmp", "gift shop", domain="style")
        args = run.call_args[0][0]
        self.assertEqual(args[0], sys.executable)
        self.assertTrue(str(args[1]).endswith("search.py"))
        self.assertIn("--domain", args)
        self.assertIn("style", args)
        self.assertNotIn("shell", run.call_args[1])
        self.assertIn("STYLE: minimal", out)
        self.assertIn("BRAND GUARD", out)

    def test_search_truncates_output(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "x" * 20000
        fake.stderr = ""
        with patch("staff.uiux_tools.find_skill_dir", return_value=Path("/skill")), \
             patch("staff.uiux_tools.subprocess.run", return_value=fake):
            out = search_uiux("/tmp", "gift shop")
        self.assertLess(len(out), 20000)
        self.assertIn(BRAND_NOTE[:20], out)

    def test_motion_patterns_all_have_vanilla_and_reduced_motion(self):
        for name, entry in MOTION_PATTERNS.items():
            self.assertIn("vanilla", entry, name)
            self.assertIn("framer_motion", entry, name)
            out = get_motion_pattern(name)
            self.assertIn("GSAP", out)
            self.assertIn("React-only", out)

    def test_motion_unknown_lists_known(self):
        out = get_motion_pattern("spin-forever")
        self.assertIn("Unknown motion pattern", out)
        self.assertIn("fade-in", out)

    def test_motion_page_transition_forbids_delaying_checkout(self):
        out = get_motion_pattern("page-transition")
        self.assertIn("never delays navigation", out)
        self.assertIn("checkout", out.lower())


if __name__ == "__main__":
    unittest.main()
