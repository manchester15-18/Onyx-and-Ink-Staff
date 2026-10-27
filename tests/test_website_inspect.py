"""Offline tests for staff.website_inspect — no browser, no network.

Screenshot rendering itself is tested live on the PC (Playwright +
Chromium), not here: these tests cover URL validation, slug/path
handling, and verdict parsing, which must be correct before any live run.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from staff.website_inspect import parse_website_answer, validate_url, _slug


class TestValidateUrl(unittest.TestCase):
    def test_accepts_https(self):
        self.assertEqual(
            validate_url("https://preview.onyx-and-ink-site.pages.dev"),
            "https://preview.onyx-and-ink-site.pages.dev",
        )

    def test_rejects_non_http(self):
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "", "onyxandink.org"):
            with self.assertRaises(ValueError, msg=bad):
                validate_url(bad)

    def test_rejects_private_targets(self):
        for bad in ("http://localhost:8765/", "http://127.0.0.1:8765/", "http://192.168.1.10/"):
            with self.assertRaises(ValueError, msg=bad):
                validate_url(bad)


class TestSlugAndParse(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(_slug("https://preview.onyx-and-ink-site.pages.dev/"), "preview-onyx-and-ink-site-pages-dev")

    def test_parse_fail(self):
        parsed = parse_website_answer(
            "1. Storefront homepage\n2. Nav readable\n3. Products visible\n"
            "4. Brand Onyx and Ink correct\n5. Yes, black and gold\n"
            "6. NONE\n7. FAIL, hero image is broken\n8. Fix the hero image."
        )
        self.assertEqual(parsed["verdict"], "fail")
        self.assertIn("hero", parsed["verdict_line"])

    def test_parse_pass(self):
        parsed = parse_website_answer(
            "1. Homepage\n2. Nav readable\n3. Products visible\n4. NONE\n"
            "5. Yes\n6. NONE\n7. PASS, clean customer view\n8. NONE."
        )
        self.assertEqual(parsed["verdict"], "pass")

    def test_parse_partial_and_raw_kept(self):
        parsed = parse_website_answer("7. PARTIAL, one image missing")
        self.assertEqual(parsed["verdict"], "partial")
        self.assertIn("PARTIAL", parsed["raw"])


if __name__ == "__main__":
    unittest.main()
