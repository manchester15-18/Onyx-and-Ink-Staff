"""Tests for the Square-link backfill tool (stage + publish, fakes only)."""
import json
import tempfile
import unittest
from pathlib import Path

from staff.square_link import link_existing_products

PRODUCTS_FIXTURE = """/* Shared catalogue test fixture. */
(function () {
  const catalogue = [
    {
      slug: 'alpha-widget',
      name: 'Alpha Widget',
      category: 'Keepsake',
      price: 1500,
      image: '/assets/alpha.webp',
      alt: 'Alpha widget',
      summary: 'Alpha summary that is long enough to be realistic.',
      description: [
        'Alpha paragraph one.'
      ],
      details: [
        'Alpha detail one',
        'Alpha detail two'
      ]
    },
    {
      slug: 'beta-widget',
      name: 'Beta Widget',
      category: 'Keepsake',
      price: 2000,
      squareId: 'beta-widget',
      image: '/assets/beta.webp',
      alt: 'Beta widget',
      summary: 'Beta summary that is long enough to be realistic.',
      description: [
        'Beta paragraph one.'
      ],
      details: [
        'Beta detail one'
      ]
    },
    {
      slug: 'bundle-x',
      name: 'Bundle X',
      category: 'Gift bundle',
      price: 4200,
      madeToOrder: true,
      checkout: true,
      image: '/assets/bundle.webp',
      alt: 'Bundle X',
      summary: 'Bundle summary that is long enough to be realistic.',
      description: [
        'Bundle paragraph one.'
      ],
      details: [
        'Bundle detail one'
      ]
    }
  ];

  const bySlug = Object.create(null);
  catalogue.forEach(function (product) {
    bySlug[product.slug] = product;
  });

  window.ONYX = { catalogue: catalogue, bySlug: bySlug };
})();
"""

SQUARE_FIXTURE = """const products={
  'beta-widget':{name:'Beta Widget'},
  'bundle-x':{name:'Bundle X',customPrice:4200}
};
export const apiFor=env=>env.SQUARE_ENVIRONMENT==='sandbox'?'https://connect.squareupsandbox.com':'https://connect.squareup.com';
export async function resolveCart(items,headers,api){ return []; }
"""


class FakeSquare:
    def __init__(self, root, mode="production"):
        self.configured = True
        self.mode = mode
        self.published = []
        self.stocked = []

    def publish_product(self, product, image_path=None):
        self.published.append(product)
        return {"square_item_id": "sq-link-1", "environment": self.mode}

    def find_variation(self, name):
        return {"item_id": "sq-link-1", "variation_id": "var-link-1", "variation_name": "Regular"}

    def set_inventory(self, name, quantity, location_id=""):
        self.stocked.append((name, quantity))
        return {"item": name, "variation_id": "var-link-1", "location_id": "loc-1",
                "before": 0, "quantity": quantity, "after": quantity, "environment": self.mode}


def _root(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    root = Path(tmp.name) / "pc"
    site = root / "storefront"
    (site / "functions" / "api").mkdir(parents=True)
    (site / "products.js").write_text(PRODUCTS_FIXTURE, encoding="utf-8")
    (site / "functions" / "api" / "_square.js").write_text(SQUARE_FIXTURE, encoding="utf-8")
    (root / "work").mkdir(parents=True)
    return root


def _staged_manifest(root):
    jobs = sorted((root / "work" / "product-jobs").glob("link-*"))
    assert len(jobs) == 1, jobs
    return jobs[0], json.loads((jobs[0] / "manifest.json").read_text(encoding="utf-8"))


class LinkStageTest(unittest.TestCase):
    def test_link_unlinked_item_stages_patch(self):
        root = _root(self)
        result = link_existing_products(root, {
            "items": [{"slug": "alpha-widget", "stock_quantity": 10}], "mode": "stage"})
        self.assertEqual(result["status"], "staged")
        job, manifest = _staged_manifest(root)
        self.assertEqual(manifest["kind"], "link")
        staged = (job / "products.js").read_text(encoding="utf-8")
        self.assertIn("squareId: 'alpha-widget'", staged)
        staged_map = (job / "_square.js").read_text(encoding="utf-8")
        self.assertIn("'alpha-widget':{name:'Alpha Widget'}", staged_map)
        self.assertIn("'beta-widget':{name:'Beta Widget'}", staged_map)
        self.assertEqual(manifest["base_shas"]["products.js"],
                         __import__("hashlib").sha256(PRODUCTS_FIXTURE.encode()).hexdigest())

    def test_already_linked_skipped_without_job(self):
        root = _root(self)
        result = link_existing_products(root, {
            "items": [{"slug": "beta-widget", "stock_quantity": 5}], "mode": "stage"})
        self.assertEqual(result["status"], "nothing_to_do")
        self.assertEqual(result["skipped"], ["beta-widget"])
        self.assertFalse((root / "work" / "product-jobs").exists())

    def test_bundle_map_preserved(self):
        root = _root(self)
        result = link_existing_products(root, {
            "items": [{"slug": "bundle-x", "stock_quantity": 3}], "mode": "stage"})
        self.assertEqual(result["status"], "staged")
        job, manifest = _staged_manifest(root)
        staged_map = (job / "_square.js").read_text(encoding="utf-8")
        self.assertIn("'bundle-x':{name:'Bundle X',customPrice:4200}", staged_map)
        staged = (job / "products.js").read_text(encoding="utf-8")
        self.assertIn("squareId: 'bundle-x'", staged)
        self.assertTrue(manifest["linked"][0]["map_preserved"])

    def test_one_of_one_link_labeling(self):
        root = _root(self)
        result = link_existing_products(root, {
            "items": [{"slug": "alpha-widget", "stock_quantity": 1, "one_of_one": True}],
            "mode": "stage"})
        self.assertEqual(result["status"], "staged")
        job, _ = _staged_manifest(root)
        staged = (job / "products.js").read_text(encoding="utf-8")
        self.assertIn("oneOfOne: true", staged)
        self.assertIn("badge: 'One of One'", staged)
        self.assertIn("'One of one — sold as is.'", staged)

    def test_one_of_one_wrong_stock_rejected(self):
        root = _root(self)
        with self.assertRaises(ValueError):
            link_existing_products(root, {
                "items": [{"slug": "alpha-widget", "stock_quantity": 5, "one_of_one": True}],
                "mode": "stage"})

    def test_unknown_slug_rejected(self):
        root = _root(self)
        with self.assertRaises(ValueError):
            link_existing_products(root, {
                "items": [{"slug": "nope-widget", "stock_quantity": 1}], "mode": "stage"})


class LinkPublishTest(unittest.TestCase):
    def test_publish_with_fake_square_ready(self):
        import staff.square_commerce as square_module
        root = _root(self)
        fake = FakeSquare(root)
        original = square_module.SquareCommerce
        square_module.SquareCommerce = lambda root: fake
        try:
            result = link_existing_products(root, {
                "items": [{"slug": "alpha-widget", "stock_quantity": 10}],
                "mode": "publish", "ceo_approved": True})
        finally:
            square_module.SquareCommerce = original
        self.assertEqual(result["status"], "ready_to_apply")
        self.assertEqual(result["square"]["alpha-widget"]["square_item_id"], "sq-link-1")
        self.assertEqual(result["square"]["alpha-widget"]["stock_after"], 10)
        self.assertEqual(fake.published[0]["title"], "Alpha Widget")
        self.assertEqual(fake.stocked, [("Alpha Widget", 10)])

    def test_publish_without_approval_stages_only(self):
        import staff.square_commerce as square_module
        root = _root(self)
        fake = FakeSquare(root)
        original = square_module.SquareCommerce
        square_module.SquareCommerce = lambda root: fake
        try:
            result = link_existing_products(root, {
                "items": [{"slug": "alpha-widget", "stock_quantity": 10}],
                "mode": "publish"})
        finally:
            square_module.SquareCommerce = original
        self.assertEqual(result["status"], "staged_pending_approval")
        self.assertEqual(fake.published, [])
        self.assertEqual(fake.stocked, [])


if __name__ == "__main__":
    unittest.main()
