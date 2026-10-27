import tempfile
import unittest
from pathlib import Path
from PIL import Image
from staff.design_tools import build_text_free_prompt, overlay_text, build_wrap, pre_publish_checklist, WRAP_WIDTH_PX, WRAP_HEIGHT_PX

class DesignToolsTests(unittest.TestCase):
    def test_text_free_prompt_locks_product_and_bans_text(self):
        p = build_text_free_prompt("Candy cane Christmas tumbler")
        self.assertIn("no text", p)
        self.assertIn("tumbler only", p)
        self.assertIn("not a can", p)
        self.assertIn("blank oval nameplate", p)
    def test_overlay_renders_real_text_and_preserves_size(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "base.png"
            Image.new("RGB", (800, 800), (250, 248, 243)).save(src)
            out = Path(d) / "out.png"
            result = overlay_text(src, out, "Onyx & Ink")
            self.assertTrue(Path(result).exists())
            with Image.open(result) as im:
                self.assertEqual(im.size, (800, 800))
    def test_wrap_is_2790x2460_at_300dpi(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "base.png"
            Image.new("RGB", (1024, 1024), (30, 27, 22)).save(src)
            out = Path(d) / "wrap.png"
            r = build_wrap(src, out)
            self.assertEqual((r["width_px"], r["height_px"]), (WRAP_WIDTH_PX, WRAP_HEIGHT_PX))
            self.assertEqual((WRAP_WIDTH_PX, WRAP_HEIGHT_PX), (2790, 2460))
            self.assertIn("NOT a verified blank", r["warning"])
            with Image.open(out) as im:
                self.assertEqual(im.size, (2790, 2460))
    def test_checklist_fails_without_product_confirm_and_with_ai_text(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "base.png"
            Image.new("RGB", (1024, 1024), (255, 255, 255)).save(src)
            passed, _ = pre_publish_checklist(src, ai_text_rendered=True, product_confirmed_tumbler=False)
            self.assertFalse(passed)
            passed2, _ = pre_publish_checklist(src, ai_text_rendered=False, product_confirmed_tumbler=True)
            self.assertTrue(passed2)

if __name__ == "__main__":
    unittest.main()
