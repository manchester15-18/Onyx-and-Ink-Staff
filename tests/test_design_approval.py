import tempfile
import unittest
from pathlib import Path
from PIL import Image
from staff.design_approval import (
    approve_design, reject_design, revoke_approval, list_approvals, get_references,
    is_approved, reference_notation,
)

class DesignApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name); (self.root / "work").mkdir()
        self.src = self.root / "work" / "d.png"
        Image.new("RGB", (1024, 1024), (0, 0, 0)).save(self.src)
    def tearDown(self):
        self.tmp.cleanup()
    def test_approve_reference_and_list(self):
        r = approve_design(self.root, self.src, "keychain", approver="Donald", notation="Standard: round, diamond, blank, no text.", as_reference=True)
        self.assertEqual(r["status"], "reference")
        self.assertTrue(r["reference"])
        self.assertTrue(is_approved(self.root, self.src))
        refs = get_references(self.root, "keychain")
        self.assertEqual(len(refs), 1)
        self.assertIn("diamond", reference_notation(self.root, "keychain"))
    def test_rejected_is_never_reference(self):
        r = reject_design(self.root, self.src, "shirt", notes="LAMON label.")
        self.assertEqual(r["status"], "rejected")
        self.assertFalse(r["reference"])
        self.assertEqual(get_references(self.root, "shirt"), [])
        self.assertFalse(is_approved(self.root, self.src))
    def test_revoke_clears_reference_but_keeps_record(self):
        approve_design(self.root, self.src, "keychain", approver="Donald", as_reference=True)
        revoke_approval(self.root, self.src, approver="James", notes="Standard changed.")
        self.assertEqual(get_references(self.root, "keychain"), [])
        self.assertEqual(list_approvals(self.root, status="revoked")[0]["status"], "revoked")
    def test_bad_status_rejected(self):
        from staff.design_approval import set_approval
        with self.assertRaises(ValueError):
            set_approval(self.root, self.src, "keychain", "gold")
    def test_reference_notation_empty_without_references(self):
        self.assertEqual(reference_notation(self.root, "puzzle"), "")

if __name__ == "__main__":
    unittest.main()
