"""Tests for deterministic tool execution in the autonomy loop."""
import json
import tempfile
import unittest
from pathlib import Path

from staff import tool_tasks
from staff.staff_tasks import enqueue, status as task_status


class FakeExecutor:
    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)

    def __call__(self, action, args, run_id):
        self.calls.append((action, args, run_id))
        if action in self.fail_on:
            return {"error": "boom"}
        return {"result": "ok-" + action}


def _root(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    root = Path(tmp.name) / "pc"
    (root / "work").mkdir(parents=True)
    return root


def _project_folder(test, body):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    folder = Path(tmp.name) / "project"
    folder.mkdir(parents=True)
    (folder / "consolidated-project-report.md").write_text(body, encoding="utf-8")
    return folder


class ToolBulletTest(unittest.TestCase):
    def test_valid_bullet_with_args(self):
        action, args, error = tool_tasks.parse_tool_bullet('- TOOL: cloudflare_d1_list {"x": 1}')
        self.assertEqual(action, "cloudflare_d1_list")
        self.assertEqual(args, {"x": 1})
        self.assertIsNone(error)

    def test_valid_bullet_without_args(self):
        action, args, error = tool_tasks.parse_tool_bullet("- TOOL: cloudflare_d1_list")
        self.assertEqual((action, args, error), ("cloudflare_d1_list", {}, None))

    def test_invalid_json_bullet(self):
        action, args, error = tool_tasks.parse_tool_bullet("- TOOL: cloudflare_d1_list {nope}")
        self.assertEqual(action, "cloudflare_d1_list")
        self.assertIsNone(args)
        self.assertIn("JSON", error)

    def test_non_tool_line(self):
        self.assertEqual(tool_tasks.parse_tool_bullet("- Ship the launch"), (None, None, None))


class DrainTest(unittest.TestCase):
    def test_queued_tool_task_executes_and_completes(self):
        root = _root(self)
        task = enqueue(root, "CEO", "Jordan", "List the D1 databases via tool",
                       "nov-1-site-launch",
                       tool={"action": "cloudflare_d1_list", "args": {}})
        fake = FakeExecutor()
        records = tool_tasks.drain_tool_tasks(root, "nov-1-site-launch", executor=fake)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "complete")
        self.assertEqual(len(fake.calls), 1)
        self.assertTrue(fake.calls[0][2].startswith("tool-task-"))
        self.assertEqual(task_status(root, 100)[0]["status"], "complete")

    def test_publish_action_without_approval_is_skipped_not_run(self):
        root = _root(self)
        enqueue(root, "Morgan", "Jordan", "Set stock via tool", "nov-1-site-launch",
                tool={"action": "square_inventory_set",
                      "args": {"item": "Keychain", "quantity": 10}})
        fake = FakeExecutor()
        records = tool_tasks.drain_tool_tasks(root, "nov-1-site-launch", executor=fake)
        self.assertEqual(records[0]["status"], "skipped")
        self.assertEqual(fake.calls, [])

    def test_publish_action_with_approval_runs(self):
        root = _root(self)
        enqueue(root, "CEO", "Jordan", "Set stock via tool", "nov-1-site-launch",
                tool={"action": "square_inventory_set",
                      "args": {"item": "Keychain", "quantity": 10},
                      "ceo_approved": True})
        fake = FakeExecutor()
        records = tool_tasks.drain_tool_tasks(root, "nov-1-site-launch", executor=fake)
        self.assertEqual(records[0]["status"], "complete")
        self.assertEqual(len(fake.calls), 1)

    def test_unknown_action_rejected(self):
        root = _root(self)
        enqueue(root, "CEO", "Jordan", "Do a mystery thing", "nov-1-site-launch",
                tool={"action": "launch_rockets", "args": {}})
        fake = FakeExecutor()
        records = tool_tasks.drain_tool_tasks(root, "nov-1-site-launch", executor=fake)
        self.assertEqual(records[0]["status"], "rejected")
        self.assertEqual(fake.calls, [])

    def test_executor_error_recorded_and_task_completed(self):
        root = _root(self)
        enqueue(root, "CEO", "Jordan", "List databases via tool", "nov-1-site-launch",
                tool={"action": "cloudflare_d1_list", "args": {}})
        fake = FakeExecutor(fail_on={"cloudflare_d1_list"})
        records = tool_tasks.drain_tool_tasks(root, "nov-1-site-launch", executor=fake)
        self.assertEqual(records[0]["status"], "failed")
        self.assertIn("boom", records[0]["summary"])
        self.assertEqual(task_status(root, 100)[0]["status"], "complete")

    def test_bullet_executes_once(self):
        root = _root(self)
        folder = _project_folder(self, "# Report\n\n## Next actions\n- TOOL: cloudflare_d1_list {}\n")
        fake = FakeExecutor()
        first = tool_tasks.drain_tool_tasks(root, "nov-1-site-launch",
                                            project_folder=folder, executor=fake)
        second = tool_tasks.drain_tool_tasks(root, "nov-1-site-launch",
                                             project_folder=folder, executor=fake)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["status"], "complete")
        self.assertEqual(second, [])
        self.assertEqual(len(fake.calls), 1)

    def test_invalid_bullet_recorded_not_retried(self):
        root = _root(self)
        folder = _project_folder(self, "## Next actions\n- TOOL: cloudflare_d1_list {bad}\n")
        fake = FakeExecutor()
        first = tool_tasks.drain_tool_tasks(root, "nov-1-site-launch",
                                            project_folder=folder, executor=fake)
        second = tool_tasks.drain_tool_tasks(root, "nov-1-site-launch",
                                             project_folder=folder, executor=fake)
        self.assertEqual(first[0]["status"], "rejected")
        self.assertEqual(second, [])
        self.assertEqual(fake.calls, [])

    def test_results_context_lists_recent_results(self):
        root = _root(self)
        enqueue(root, "CEO", "Jordan", "List databases via tool", "nov-1-site-launch",
                tool={"action": "cloudflare_d1_list", "args": {}})
        tool_tasks.drain_tool_tasks(root, "nov-1-site-launch", executor=FakeExecutor())
        context = tool_tasks.tool_results_context(root)
        self.assertIn("cloudflare_d1_list", context)
        self.assertIn("complete", context)


if __name__ == "__main__":
    unittest.main()

class LinkExecutorTest(unittest.TestCase):
    def test_link_action_passes_approval_flag_through(self):
        root = _root(self)
        enqueue(root, "CEO", "Jordan", "Link catalog via tool", "nov-1-site-launch",
                tool={"action": "square_link_catalog",
                      "args": {"items": [{"slug": "alpha-widget", "stock_quantity": 1}]},
                      "ceo_approved": True})
        fake = FakeExecutor()
        records = tool_tasks.drain_tool_tasks(root, "nov-1-site-launch", executor=fake)
        self.assertEqual(records[0]["status"], "complete")
        self.assertTrue(fake.calls[0][1]["ceo_approved"])
