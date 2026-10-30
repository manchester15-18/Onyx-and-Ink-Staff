"""Tests for the new-product pipeline (stage + publish, fakes only)."""
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from staff import product_pipeline
from staff.product_pipeline import create_product


def _png_bytes(width=64, height=48, color=(200, 30, 30)):
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _spec(test, **overrides):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    files = []
    for index in range(5):
        path = Path(tmp.name) / ("img%d.png" % index)
        path.write_bytes(_png_bytes())
        files.append(str(path))
    spec = {
        "slug": "test-widget",
        "name": "Test Widget",
        "category": "Keepsake",
        "price_cents": 1500,
        "stock_quantity": 25,
        "summary": "A test widget for the pipeline, small and bright and ready to gift.",
        "description": ["A small bright widget made for testing the pipeline end to end."],
        "details": ["Test detail one", "Test detail two"],
        "keywords": ["widget", "gift", "test"],
        "image_files": files,
        "mode": "stage",
    }
    spec.update(overrides)
    return spec


def _root(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    root = Path(tmp.name) / "pc"
    (root / "work").mkdir(parents=True)
    return root


class FakeSquare:
    def __init__(self, root, mode="production"):
        self.configured = True
        self.mode = mode
        self.published = []
        self.stocked = []

    def publish_product(self, product, image_path=None):
        self.published.append(product)
        return {"square_item_id": "sq-123", "environment": self.mode}

    def find_variation(self, name):
        return {"item_id": "sq-123", "variation_id": "var-1", "variation_name": "Regular"}

    def set_inventory(self, name, quantity, location_id=""):
        self.stocked.append((name, quantity))
        return {"item": name, "variation_id": "var-1", "location_id": "loc-1",
                "before": 0, "quantity": quantity, "after": quantity, "environment": self.mode}


class ValidationTest(unittest.TestCase):
    def test_bad_slug_rejected(self):
        root = _root(self)
        with self.assertRaises(ValueError):
            create_product(root, _spec(self, slug="Bad Slug!"))

    def test_zero_price_rejected(self):
        root = _root(self)
        with self.assertRaises(ValueError):
            create_product(root, _spec(self, price_cents=0))

    def test_four_images_rejected(self):
        root = _root(self)
        spec = _spec(self)
        spec["image_files"] = spec["image_files"][:4]
        with self.assertRaises(ValueError):
            create_product(root, spec)

    def test_both_prompts_and_files_rejected(self):
        root = _root(self)
        spec = _spec(self)
        spec["image_prompts"] = ["prompt %d for a widget photo" % i for i in range(5)]
        with self.assertRaises(ValueError):
            create_product(root, spec)

    def test_short_summary_rejected(self):
        root = _root(self)
        with self.assertRaises(ValueError):
            create_product(root, _spec(self, summary="Too short."))

    def test_slug_collision_rejected(self):
        root = _root(self)
        site = root / "storefront"
        (site / "functions" / "api").mkdir(parents=True)
        (site / "products.js").write_text("  { slug: 'test-widget', name: 'Old' },\n", encoding="utf-8")
        (site / "functions" / "api" / "_square.js").write_text(
            "const products={\n  'test-widget':{name:'Test Widget'},\n};\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            create_product(root, _spec(self))


class StageTest(unittest.TestCase):
    def test_stage_mode_converts_images_and_stages_manifest(self):
        root = _root(self)
        result = create_product(root, _spec(self))
        self.assertEqual(result["status"], "staged")
        self.assertIsNone(result["square_item_id"])
        job = root / "work" / "product-jobs" / "test-widget"
        manifest = json.loads((job / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "staged")
        self.assertEqual(manifest["entry"]["price"], 1500)
        self.assertEqual(manifest["entry"]["squareId"], "test-widget")
        self.assertEqual(len(manifest["entry"]["gallery"]), 5)
        self.assertEqual(manifest["entry"]["gallery"][0], "/assets/test-widget.webp")
        self.assertEqual(manifest["entry"]["seo"]["keywords"], ["widget", "gift", "test"])
        self.assertIsNone(manifest["square"])
        for filename in ("test-widget.webp", "test-widget-view-2.webp", "test-widget-view-5.webp"):
            path = job / "assets" / filename
            self.assertTrue(path.is_file(), filename)
            with Image.open(path) as image:
                self.assertEqual(image.format, "WEBP")
                self.assertEqual(image.size, (64, 48))


class PublishTest(unittest.TestCase):
    def test_publish_without_approval_stages_only(self):
        root = _root(self)
        result = create_product(root, _spec(self, mode="publish"))
        self.assertEqual(result["status"], "staged_pending_approval")
        manifest = json.loads((root / "work" / "product-jobs" / "test-widget" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "staged_pending_approval")
        self.assertIsNone(manifest["square"])

    def test_publish_with_fake_square_ready_to_apply(self):
        import staff.square_commerce as square_module
        root = _root(self)
        fake = FakeSquare(root)
        original = square_module.SquareCommerce
        square_module.SquareCommerce = lambda root: fake
        try:
            result = create_product(root, _spec(self, mode="publish", ceo_approved=True))
        finally:
            square_module.SquareCommerce = original
        self.assertEqual(result["status"], "ready_to_apply")
        self.assertEqual(result["square_item_id"], "sq-123")
        self.assertEqual(result["stock_after"], 25)
        self.assertEqual(fake.published[0]["title"], "Test Widget")
        self.assertEqual(fake.stocked, [("Test Widget", 25)])

    def test_publish_sandbox_refused_and_manifest_failed(self):
        import staff.square_commerce as square_module
        root = _root(self)
        fake = FakeSquare(root, mode="sandbox")
        original = square_module.SquareCommerce
        square_module.SquareCommerce = lambda root: fake
        try:
            with self.assertRaises(RuntimeError):
                create_product(root, _spec(self, mode="publish", ceo_approved=True))
        finally:
            square_module.SquareCommerce = original
        manifest = json.loads((root / "work" / "product-jobs" / "test-widget" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(fake.published, [])


if __name__ == "__main__":
    unittest.main()

"""Tests for one-of-one and made-to-order support in product_create."""


class OneOfOneTest(unittest.TestCase):
    def test_one_of_one_entry_labeling(self):
        root = _root(self)
        result = create_product(root, _spec(self, one_of_one=True, stock_quantity=1))
        self.assertEqual(result["status"], "staged")
        entry = result["entry"]
        self.assertTrue(entry["oneOfOne"])
        self.assertEqual(entry["badge"], "One of One")
        self.assertEqual(entry["details"][0], "One of one — sold as is.")

    def test_one_of_one_wrong_stock_rejected(self):
        root = _root(self)
        with self.assertRaises(ValueError):
            create_product(root, _spec(self, one_of_one=True, stock_quantity=5))

    def test_one_of_one_badge_conflict_rejected(self):
        root = _root(self)
        with self.assertRaises(ValueError):
            create_product(root, _spec(self, one_of_one=True, stock_quantity=1, badge="New"))


class MadeToOrderTest(unittest.TestCase):
    def test_made_to_order_allowed_and_linked(self):
        root = _root(self)
        result = create_product(root, _spec(self, made_to_order=True))
        self.assertEqual(result["status"], "staged")
        self.assertTrue(result["entry"]["madeToOrder"])
        self.assertEqual(result["entry"]["squareId"], "test-widget")

"""Tests for personalization options in product_create."""


class OptionsTest(unittest.TestCase):
    def test_options_entry_normalized(self):
        root = _root(self)
        result = create_product(root, _spec(self, options=[
            {"id": "tumbler-color", "label": "Tumbler color", "type": "radio",
             "required": True, "choices": ["Matte Black", "Cream"]},
            {"id": "gift-wrap", "label": "Gift wrap", "type": "select",
             "choices": ["No gift wrap", "Kraft box"]},
            {"id": "engraving", "label": "Engraving text", "type": "text",
             "maxLength": 20, "placeholder": "Name or short phrase"},
        ]))
        options = result["entry"]["options"]
        self.assertEqual(len(options), 3)
        self.assertEqual(options[0]["choices"], [{"value": "Matte Black"}, {"value": "Cream"}])
        self.assertTrue(options[0]["required"])
        self.assertEqual(options[2]["maxLength"], 20)
        self.assertEqual(options[2]["placeholder"], "Name or short phrase")

    def test_options_bad_type_rejected(self):
        root = _root(self)
        with self.assertRaises(ValueError):
            create_product(root, _spec(self, options=[
                {"id": "x", "label": "X", "type": "checkbox"}]))

    def test_options_single_choice_rejected(self):
        root = _root(self)
        with self.assertRaises(ValueError):
            create_product(root, _spec(self, options=[
                {"id": "x", "label": "X", "type": "radio", "choices": ["Only"]}]))

    def test_options_duplicate_id_rejected(self):
        root = _root(self)
        with self.assertRaises(ValueError):
            create_product(root, _spec(self, options=[
                {"id": "x", "label": "X", "type": "text"},
                {"id": "x", "label": "Y", "type": "text"}]))
