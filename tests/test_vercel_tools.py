"""Offline tests for staff/vercel_tools.py (no network, no container)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from staff.vercel_tools import (
    BRAND_NOTE,
    GUIDELINES_URL,
    get_vercel_skill,
    prepare_vercel_audit,
    refresh_vercel_snapshot,
)


def _fake_root(tmp):
    root = Path(tmp) / "work" / "vercel-design-guidelines"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("name: web-design-guidelines\n", encoding="utf-8")
    (root / "command.md").write_text("- Icon-only buttons need `aria-label`\n", encoding="utf-8")
    (root / "SNAPSHOT.txt").write_text(
        "Snapshot taken: 2026-10-27T21:24:57Z\ncommand.md size: 8055 bytes, 104 bullet rules\n",
        encoding="utf-8",
    )
    return root


class TestVercelTools(unittest.TestCase):
    def test_missing_dir_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = get_vercel_skill(tmp)
            self.assertIn("not found", out)
            self.assertIn("vercel-design-guidelines", out)

    def test_skill_returns_guard_and_vanilla_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fake_root(tmp)
            out = get_vercel_skill(tmp, include_rules=True)
            self.assertIn("web-design-guidelines", out)
            self.assertIn("aria-label", out)
            self.assertIn("React-only", out)
            self.assertIn("REVIEW-ONLY", out)
            self.assertIn("rich Onyx green", out)

    def test_refresh_writes_snapshot_from_local_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_root(tmp)
            rules = Path(tmp) / "rules.md"
            rules.write_text("- Test rule one\n- Test rule two\n", encoding="utf-8")
            out = refresh_vercel_snapshot(tmp, url=rules.as_uri())
            self.assertIn("refreshed", out)
            self.assertIn("2 bullet rules", out)
            self.assertIn("SHA-256", out)
            snap = (root / "SNAPSHOT.txt").read_text(encoding="utf-8")
            self.assertIn(GUIDELINES_URL, snap)
            self.assertIn("2 bullet rules", snap)

    def test_refresh_failure_reports_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fake_root(tmp)
            out = refresh_vercel_snapshot(tmp, url="file:///nonexistent-rules-xyz.md")
            self.assertIn("FAILED", out)

    def test_audit_rejects_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fake_root(tmp)
            out = prepare_vercel_audit(tmp, "https://example.com/index.html")
            self.assertIn("rejected", out)

    def test_audit_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fake_root(tmp)
            out = prepare_vercel_audit(tmp, "../outside.html")
            self.assertIn("rejected", out)

    def test_audit_rejects_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fake_root(tmp)
            out = prepare_vercel_audit(tmp, "storefront/nope.html")
            self.assertIn("does not exist", out)

    def test_audit_returns_file_content_and_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fake_root(tmp)
            target = Path(tmp) / "storefront" / "index.html"
            target.parent.mkdir(parents=True)
            target.write_text("<button>Menu</button>\n", encoding="utf-8")
            out = prepare_vercel_audit(tmp, "storefront/index.html")
            self.assertIn("VERCEL AUDIT PREP", out)
            self.assertIn("Snapshot taken: 2026-10-27", out)
            self.assertIn("<button>Menu</button>", out)
            self.assertIn("review-only", out.lower())
            self.assertIn("React-only", out)

    def test_audit_bare_filename_resolves_to_storefront(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fake_root(tmp)
            target = Path(tmp) / "storefront" / "index.html"
            target.parent.mkdir(parents=True)
            target.write_text("<button>Menu</button>\n", encoding="utf-8")
            out = prepare_vercel_audit(tmp, "index.html")
            self.assertIn("VERCEL AUDIT PREP", out)
            self.assertIn("<button>Menu</button>", out)

    def test_brand_note_covers_storefront(self):
        self.assertIn("vanilla HTML/CSS/JS", BRAND_NOTE)
        self.assertIn("JORDAN_SITE_WORKSPACE", BRAND_NOTE)


if __name__ == "__main__":
    unittest.main()
