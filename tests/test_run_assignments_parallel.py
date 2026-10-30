"""Parallel direct-assignment test for main.run_assignments (Oct 28, 2026).

No live provider calls: each agent's LLM is a stub that sleeps 0.5s
and returns a valid report. Sequential execution would therefore take
~2.0s for four agents; the parallel direct path must finish well under
that. Task claim/complete are no-ops here (assigned_task is None), so
this test writes only to its own temporary report files.
"""
import tempfile
import time
import unittest
from pathlib import Path

import main
from main import DirectAutonomousAssignment, run_assignments

REPORT = (
    "Summary\n- Work done.\n"
    "Work completed\n- Drafted the deliverable.\n"
    "Assumptions and risks\n- None recorded.\n"
    "Next actions\n- Next owner: review.\n"
    "Decisions needed\n- None recorded.\n"
)


class _SlowStubLLM:
    def call(self, messages):
        time.sleep(0.5)
        return REPORT


class RunAssignmentsParallelTests(unittest.TestCase):
    def test_four_direct_assignments_run_concurrently(self):
        with tempfile.TemporaryDirectory() as folder:
            tasks = {}
            for name in ("Morgan", "Avery", "Jordan", "Cameron"):
                tasks[name] = DirectAutonomousAssignment(
                    _SlowStubLLM(), name, "Continue approved work.",
                    "Produce one deliverable.", Path(folder) / (name + ".md"))
            started = time.monotonic()
            outputs = run_assignments(tasks)
            elapsed = time.monotonic() - started
            self.assertEqual(set(outputs), set(tasks))
            for name, task in tasks.items():
                self.assertTrue(Path(task.output_file).exists())
            # Sequential would be ~2.0s (4 x 0.5s). Parallel must be
            # close to a single agent's time, with generous headroom.
            self.assertLess(elapsed, 1.5, "direct assignments ran sequentially")


if __name__ == "__main__":
    unittest.main()
