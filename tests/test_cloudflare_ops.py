"""Tests for bounded Cloudflare D1/Pages ops + Square inventory (Oct 30, 2026).

No live API calls: Cloudflare HTTP is stubbed through _request, Square
HTTP through _request, and SquareCommerce reads a temp .env. Covers
the safety bounds that make these tools agent-safe: D1 name allowlist,
SQL allowlist, binding merge preserving other environments (and
restore on mismatch), D1 create idempotency, and the Square
production-only gate for stock.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from staff.cloudflare_ops import CloudflareOps, validate_database_name, validate_sql
from staff.square_commerce import SquareCommerce


class FakeOps(CloudflareOps):
    """CloudflareOps with canned API responses, no network."""

    def __init__(self, project_state):
        self.project_state = project_state
        self.patches = []
        self.token = "x"
        self.account = "acct"
        self.project = "onyx-and-ink"
        self.base = "https://example.invalid"

    def _request(self, method, path, payload=None):
        if method == "GET" and path.endswith("/d1/database?per_page=100"):
            return [{"uuid": "uuid-preview", "name": "onyx-and-ink-preview", "created_at": "2026-10-23"}]
        if method == "GET" and "/d1/database/" in path:
            return {"uuid": path.rsplit("/", 1)[-1], "name": "onyx-ink-prod"}
        if method == "POST" and path.endswith("/d1/database"):
            return {"uuid": "11111111-2222-3333-4444-555555555555", "name": payload["name"], "created_at": "2026-10-30"}
        if method == "GET" and "/pages/projects/" in path:
            import copy as _copy
            return {"name": "onyx-and-ink", "domains": [],
                    "deployment_configs": _copy.deepcopy(self.project_state)}
        if method == "PATCH" and "/pages/projects/" in path:
            self.patches.append(payload["deployment_configs"])
            import copy as _copy
            self.project_state = _copy.deepcopy(payload["deployment_configs"])
            return {"name": "onyx-and-ink"}
        if method == "POST" and "/query" in path:
            return [{"results": [{"name": "accounts"}], "meta": {}, "success": True}]
        raise AssertionError("unexpected call %s %s" % (method, path))


def _project_state():
    return {"production": {"d1_databases": {}},
            "preview": {"d1_databases": {"DB": {"id": "uuid-preview"}}}}


class CloudflareOpsTest(unittest.TestCase):
    def test_name_allowlist(self):
        self.assertEqual(validate_database_name("onyx-ink-prod"), "onyx-ink-prod")
        for bad in ("prod", "onyx_ink", "Onyx-Ink", "onyx-", "other-db", ""):
            with self.assertRaises(ValueError):
                validate_database_name(bad)

    def test_sql_allowlist(self):
        self.assertTrue(validate_sql("SELECT name FROM sqlite_master").startswith("SELECT"))
        self.assertTrue(validate_sql("CREATE TABLE accounts (id TEXT PRIMARY KEY)").startswith("CREATE"))
        for bad in ("DROP TABLE accounts", "DELETE FROM accounts", "UPDATE accounts SET x=1",
                    "ALTER TABLE accounts ADD COLUMN x TEXT", "PRAGMA table_info(accounts)",
                    "SELECT 1; DROP TABLE accounts"):
            with self.assertRaises(ValueError):
                validate_sql(bad)

    def test_create_is_idempotent_for_existing_name(self):
        ops = FakeOps(_project_state())
        result = ops.create_database("onyx-and-ink-preview")
        self.assertFalse(result["created"])
        self.assertEqual(result["database"]["uuid"], "uuid-preview")

    def test_create_new_database(self):
        ops = FakeOps(_project_state())
        result = ops.create_database("onyx-ink-prod")
        self.assertTrue(result["created"])
        self.assertEqual(result["database"]["uuid"], "11111111-2222-3333-4444-555555555555")

    def test_bind_merges_and_preserves_preview(self):
        ops = FakeOps(_project_state())
        result = ops.bind_d1("11111111-2222-3333-4444-555555555555", "production", "DB")
        self.assertEqual(result["after"], {"DB": {"id": "11111111-2222-3333-4444-555555555555"}})
        self.assertTrue(result["other_environments_preserved"])
        self.assertEqual(ops.project_state["preview"]["d1_databases"], {"DB": {"id": "uuid-preview"}})
        self.assertEqual(ops.project_state["production"]["d1_databases"], {"DB": {"id": "11111111-2222-3333-4444-555555555555"}})

    def test_bind_restores_on_verification_mismatch(self):
        class LyingOps(FakeOps):
            def get_project(self, name=""):
                # Pretend the PATCH clobbered preview, forcing a restore.
                state = super().get_project(name)
                if self.patches:
                    state["d1_databases"]["preview"] = {}
                return state

        ops = LyingOps(_project_state())
        with self.assertRaises(RuntimeError):
            ops.bind_d1("11111111-2222-3333-4444-555555555555", "production", "DB")
        # Two patches: the bind, then the restore of the original configs.
        self.assertEqual(len(ops.patches), 2)
        self.assertEqual(ops.patches[1]["preview"]["d1_databases"], {"DB": {"id": "uuid-preview"}})

    def test_query_uses_resolved_uuid(self):
        ops = FakeOps(_project_state())
        result = ops.query("onyx-and-ink-preview", "SELECT name FROM sqlite_master")
        self.assertEqual(result["results"], [{"name": "accounts"}])


class SquareInventoryTest(unittest.TestCase):
    def _commerce(self, environment):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / ".env").write_text(
            "SQUARE_ACCESS_TOKEN=test-token\nSQUARE_ENVIRONMENT=%s\n" % environment)
        return SquareCommerce(root)

    def test_sandbox_refuses_production_stock(self):
        commerce = self._commerce("sandbox")
        with self.assertRaises(RuntimeError):
            commerce.set_inventory("Keychain", 10)

    def test_production_physical_count_payload_and_roundtrip(self):
        commerce = self._commerce("production")
        seen = {}

        def fake_request(method, path, **kwargs):
            seen.setdefault("calls", []).append((method, path, kwargs.get("json")))
            if path == "/catalog/list?types=ITEM":
                return {"objects": [{"type": "ITEM", "id": "item-1",
                                     "item_data": {"name": "Keychain"}}]}
            if path == "/catalog/object/item-1":
                return {"object": {"id": "item-1", "item_data": {"variations": [
                    {"id": "var-1", "item_variation_data": {"name": "Regular"}}]}}}
            if path.startswith("/inventory/counts"):
                return {"counts": [{"state": "IN_STOCK", "quantity": "0"}]}
            if path == "/inventory/changes":
                return {"changes": [{"type": "PHYSICAL_COUNT"}]}
            raise AssertionError(path)

        with patch.object(SquareCommerce, "_request", side_effect=fake_request):
            result = commerce.set_inventory("Keychain", 10, location_id="loc-1")
        self.assertEqual(result["variation_id"], "var-1")
        self.assertEqual(result["quantity"], 10)
        change = [c for c in seen["calls"] if c[1] == "/inventory/changes"][0][2]
        physical = change["changes"][0]["physical_count"]
        self.assertEqual(physical["quantity"], "10")
        self.assertEqual(physical["location_id"], "loc-1")


if __name__ == "__main__":
    unittest.main()
