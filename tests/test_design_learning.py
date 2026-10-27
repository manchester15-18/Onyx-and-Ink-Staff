import json
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from staff.design_learning import (
    add_lesson, learned_lessons_for, learned_suffix, full_learned_prompt, critique_design,
)

class DesignLearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "work").mkdir()
    def tearDown(self):
        self.tmp.cleanup()
    def test_lesson_dedupe_and_cap(self):
        for i in range(7):
            add_lesson(self.root, "tumbler", f"lesson {i}")
        lessons = learned_lessons_for(self.root, "tumbler")
        self.assertEqual(len([l for l in lessons if l.startswith("lesson")]), 5)
        add_lesson(self.root, "tumbler", "lesson 6")
        self.assertEqual(learned_lessons_for(self.root, "tumbler").count("lesson 6"), 1)
    def test_learned_prompt_includes_lessons(self):
        add_lesson(self.root, "keychain", "gold diamond accent works")
        p = full_learned_prompt(self.root, "Black keychain", product="keychain")
        self.assertIn("gold diamond accent works", p)
        self.assertIn("no text", p)
    def test_critique_saves_record_and_lesson(self):
        src = self.root / "work" / "test.png"
        Image.new("RGB", (1024, 1024), (0, 0, 0)).save(src)
        rec = critique_design(self.root, src, "shirt", "partial", "Good art.", "Neck label text.", suggested_fix="no text on neck label", text_found=True)
        self.assertEqual(rec["verdict"], "partial")
        self.assertTrue(rec["checks"]["file_exists"])
        critiques = (self.root / "work" / "design-critiques.jsonl").read_text()
        self.assertIn("Neck label text.", critiques)
        self.assertIn("no text on neck label", learned_lessons_for(self.root, "shirt"))
    def test_critique_rejects_bad_verdict(self):
        src = self.root / "work" / "test.png"
        Image.new("RGB", (1024, 1024), (0, 0, 0)).save(src)
        with self.assertRaises(ValueError):
            critique_design(self.root, src, "shirt", "great", "x", "y")

if __name__ == "__main__":
    unittest.main()
