"""Tests for CEO/VP decision intake (Oct 30, 2026).

Covers: option parsing, newest-reply extraction, and the three ways a
blocked chain is resolved (accept / hold / reassign), plus idempotency
and the guards that stop non-authorities and mismatched assignments
from moving a blocked chain. Fixtures live in a temp dir; no live
state, mail, or provider calls are touched.
"""

import json
import tempfile
import unittest
from pathlib import Path

from staff.ceo_decisions import (
    apply_decision,
    extract_assignment_id,
    extract_newest_reply,
    handle_decision_email,
    is_decision_thread,
    parse_decision_text,
)

ASSIGNMENT = "20261029-075355-63558a"
PROJECT = "nov-1-site-launch"


def _handoff(stage="decision_packet"):
    return {
        "project": PROJECT,
        "stage": stage,
        "owner": "Morgan",
        "task": "Maintain the launch register.",
        "assignment_id": ASSIGNMENT,
        "retries": 2,
        "max_retries": 2,
        "corrections": [
            "Morgan: remove reasoning/meta-commentary from the report and lead with the deliverable itself.",
            "Cameron: deliverable is too thin -- sections filled only with 'None recorded' do not count.",
        ],
        "decision_packet": {
            "options": [
                "Accept the current drafts as-is, with the QC gaps noted",
                "Hold for CEO direction on the blocker below",
                "Reassign with a narrowed scope and the QC corrections applied",
            ],
            "recommendation": "Hold for CEO direction on the blocker below",
            "impact": "Drafts remain drafts.",
            "approval_needed": "Approve one option above.",
        },
        "agents_delivered": [],
    }


class DecisionIntakeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "work").mkdir(parents=True)
        self._write_state()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_state(self, handoff=None):
        state = {"enabled": True, "runNowPending": False,
                 "objective": "Launch.", "handoff": handoff if handoff is not None else _handoff()}
        (self.root / "work" / "staff-autonomy.json").write_text(json.dumps(state))
        (self.root / "work" / "staff-work-status.json").write_text(
            json.dumps({"status": "waiting", "handoff": state["handoff"]}))
        (self.root / "work" / "staff-task-queue.json").write_text(json.dumps({"tasks": []}))
        (self.root / "work" / "report-projects.json").write_text(
            json.dumps({"active": PROJECT, "labels": {PROJECT: "Nov 1 Site Launch"}}))

    def _read_state(self):
        return json.loads((self.root / "work" / "staff-autonomy.json").read_text())

    def _tasks(self):
        return json.loads((self.root / "work" / "staff-task-queue.json").read_text())["tasks"]

    def test_parse_options(self):
        self.assertEqual(parse_decision_text("Reassign with a narrowed scope, please."), "reassign")
        self.assertEqual(parse_decision_text("Accept the drafts as-is."), "accept")
        self.assertEqual(parse_decision_text("Hold for now."), "hold")
        self.assertIsNone(parse_decision_text("Please proceed."))
        self.assertIsNone(parse_decision_text(""))

    def test_extract_newest_strips_quoted_thread(self):
        body = ("Reassign please.\n\nOn Thu, Oct 29, 2026 at 8:15 AM Morgan wrote:\n"
                "> Assignment: " + ASSIGNMENT + "\n> Old packet text")
        self.assertEqual(extract_newest_reply(body), "Reassign please.")
        self.assertEqual(extract_assignment_id(body), ASSIGNMENT)

    def test_is_decision_thread(self):
        self.assertTrue(is_decision_thread("Re: CEO approval needed: Nov 1 Site Launch"))
        self.assertFalse(is_decision_thread("Weekly report"))

    def test_reassign_queues_corrections_and_wakes_loop(self):
        result = apply_decision(self.root, PROJECT, ASSIGNMENT, "reassign",
                                source="chat", sender_role="CEO")
        self.assertTrue(result["applied"])
        tasks = self._tasks()
        self.assertEqual(len(tasks), 2)
        self.assertEqual({t["to"] for t in tasks}, {"Morgan", "Cameron"})
        self.assertTrue(all(t["status"] == "pending" for t in tasks))
        state = self._read_state()
        self.assertEqual(state["handoff"]["stage"], "qc_retry_1")
        self.assertEqual(state["handoff"]["retries"], 1)
        self.assertTrue(state["runNowPending"])
        self.assertEqual(state["handoff"]["decision_resolved"], "reassign")
        # Idempotency: a second apply queues nothing twice.
        again = apply_decision(self.root, PROJECT, ASSIGNMENT, "reassign",
                               source="chat", sender_role="CEO")
        self.assertFalse(again["applied"])
        self.assertEqual(again["reason"], "already_applied")
        self.assertEqual(len(self._tasks()), 2)

    def test_accept_closes_chain_as_qc_pass(self):
        result = apply_decision(self.root, PROJECT, ASSIGNMENT, "accept",
                                source="chat", sender_role="CEO")
        self.assertTrue(result["applied"])
        state = self._read_state()
        self.assertEqual(state["handoff"]["stage"], "qc_pass")
        self.assertEqual(state["handoff"]["corrections"], [])
        self.assertEqual(len(self._tasks()), 0)

    def test_hold_keeps_chain_stopped(self):
        result = apply_decision(self.root, PROJECT, ASSIGNMENT, "hold",
                                source="chat", sender_role="VP")
        self.assertTrue(result["applied"])
        state = self._read_state()
        self.assertEqual(state["handoff"]["stage"], "decision_packet")
        self.assertEqual(state["handoff"]["decision_resolved"], "hold")
        self.assertEqual(len(self._tasks()), 0)

    def test_guards_reject_non_authority_mismatch_and_unblocked(self):
        bad = apply_decision(self.root, PROJECT, ASSIGNMENT, "reassign",
                             source="chat", sender_role="Staff")
        self.assertFalse(bad["applied"])
        wrong = apply_decision(self.root, PROJECT, "other-assignment", "reassign",
                               source="chat", sender_role="CEO")
        self.assertFalse(wrong["applied"])
        self.assertEqual(len(self._tasks()), 0)
        self._write_state(handoff=_handoff(stage="qc_pass"))
        unblocked = apply_decision(self.root, PROJECT, ASSIGNMENT, "reassign",
                                   source="chat", sender_role="CEO")
        self.assertFalse(unblocked["applied"])

    def test_handle_decision_email_applies_and_ignores_non_decisions(self):
        body = ("Reassign with a narrowed scope and the QC corrections applied.\n\n"
                "On Thu, Oct 29, 2026 at 8:15 AM Morgan wrote:\n> Assignment: " + ASSIGNMENT)
        msg = {"Subject": "Re: CEO approval needed: Nov 1 Site Launch"}
        confirmation = handle_decision_email(self.root, None, msg, body, "CEO")
        self.assertIsNotNone(confirmation)
        self.assertIn("Reassigned", confirmation)
        self.assertEqual(self._read_state()["handoff"]["stage"], "qc_retry_1")
        # Non-decision mail and non-authority senders fall through (None).
        self._write_state()
        self.assertIsNone(handle_decision_email(
            self.root, None, {"Subject": "Weekly report"}, "Reassign please.", "CEO"))
        self.assertIsNone(handle_decision_email(
            self.root, None, msg, body, "Staff"))


if __name__ == "__main__":
    unittest.main()
