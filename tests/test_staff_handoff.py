"""Tests for the automatic Morgan handoff chain (staff_handoff.py).

Pure logic only: no model calls, no network, no production side effects.
Covers the full required sequence — assign -> deliver -> QC-fail ->
correction -> QC-pass — and the decision-packet path after the retry
bound, plus the CEO-block shortcut and the forbidden-claim scan.
"""
import tempfile
import unittest
from pathlib import Path

from staff.staff_handoff import (
    HandoffChain, MAX_QC_RETRIES, build_decision_packet, qc_report,
)

GOOD = (
    "Summary\n- Dependency register updated.\n"
    "Work completed\n- Drafted the register.\n"
    "Assumptions and risks\n- None recorded.\n"
    "Next actions\n- Jordan: verify the preview checklist.\n"
    "Decisions needed\n- None recorded.\n"
)
BAD = "A note with no sections at all and no usable deliverable content."


def _reports(**overrides):
    reports = {name: GOOD for name in ("Morgan", "Avery", "Jordan", "Cameron")}
    reports.update(overrides)
    return reports


class HandoffChainTests(unittest.TestCase):
    def test_assign_deliver_qc_fail_correction_qc_pass(self):
        chain = HandoffChain("nov-1-site-launch")
        chain.assign("Draft the dependency register.", "Morgan", "test-1")
        self.assertEqual(chain.state()["stage"], "assigned")
        chain.deliver("Avery", BAD)
        self.assertEqual(chain.state()["stage"], "delivered")
        stage = chain.run_qc(_reports(Avery=BAD))
        self.assertEqual(stage, "qc_retry_1")
        corrections = chain.state()["corrections"]
        # Corrections must be specific, not a generic "try again".
        self.assertTrue(any("Avery" in item for item in corrections))
        self.assertTrue(any("Next action" in item for item in corrections))
        self.assertIn("correction", chain.correction_directive().lower())
        # Agent applies the corrections and re-delivers; QC now passes.
        chain.assign("Draft the dependency register.", "Morgan", "test-1",
                     keep_retries=True)
        chain.deliver("Avery", GOOD)
        self.assertEqual(chain.run_qc(_reports()), "qc_pass")
        self.assertEqual(chain.state()["corrections"], [])

    def test_decision_packet_after_retry_bound(self):
        chain = HandoffChain("nov-1-site-launch")
        chain.assign("Draft the dependency register.", "Morgan", "test-2")
        # Initial delivery + MAX_QC_RETRIES corrections = 3 deliveries.
        # The third failed QC must produce a packet, not a fourth delivery.
        self.assertEqual(MAX_QC_RETRIES, 2)
        chain.deliver("Avery", BAD)
        self.assertEqual(chain.run_qc(_reports(Avery=BAD)), "qc_retry_1")
        chain.assign("Draft the dependency register.", "Morgan", "test-2",
                     keep_retries=True)
        chain.deliver("Avery", BAD)
        self.assertEqual(chain.run_qc(_reports(Avery=BAD)), "qc_retry_2")
        chain.assign("Draft the dependency register.", "Morgan", "test-2",
                     keep_retries=True)
        chain.deliver("Avery", BAD)
        self.assertEqual(chain.run_qc(_reports(Avery=BAD)), "decision_packet")
        packet = chain.state()["decision_packet"]
        self.assertIn("options", packet)
        self.assertIn("recommendation", packet)
        self.assertIn("impact", packet)
        self.assertTrue(packet["approval_needed"])
        # The packet invents no business facts: it only names the QC gaps.
        self.assertNotIn("$", packet["approval_needed"])

    def test_ceo_block_goes_straight_to_decision_packet(self):
        chain = HandoffChain("nov-1-site-launch")
        chain.assign("Finalise launch stock.", "Morgan", "test-3")
        chain.deliver("Cameron", GOOD)
        stage = chain.blocked_by_ceo_decision(
            "Production stock quantities need James's approval.")
        self.assertEqual(stage, "decision_packet")
        packet = chain.state()["decision_packet"]
        self.assertIn("Production stock quantities", packet["approval_needed"])
        self.assertEqual(chain.state()["retries"], 0)

    def test_blocked_report_text_goes_straight_to_decision_packet(self):
        chain = HandoffChain("nov-1-site-launch")
        chain.assign("Finalise launch stock.", "Morgan", "test-4")
        blocked = ("No further useful work is possible without a CEO "
                   "decision on stock quantities.")
        self.assertEqual(chain.run_qc(_reports(Cameron=blocked)),
                         "decision_packet")

    def test_forbidden_claim_flagged_but_approval_requirement_is_not(self):
        claimed = GOOD + "Work completed\n- Published to production today.\n"
        self.assertTrue(any("production" in item.lower()
                            for item in qc_report("Jordan", claimed)))
        required = GOOD + ("Decisions needed\n- Merge to main requires "
                           "James explicit approval.\n")
        self.assertEqual(qc_report("Jordan", required), [])

    def test_good_standard_report_passes(self):
        self.assertEqual(qc_report("Morgan", GOOD), [])

    def test_thin_none_recorded_report_fails_qc(self):
        thin = ("Summary\n- Draft Customer\n"
                "Work completed\n- None recorded.\n"
                "Next actions\n- None recorded.\n"
                "Decisions needed\n- None recorded.\n")
        self.assertTrue(qc_report("Cameron", thin))

    def test_meta_commentary_report_fails_qc(self):
        meta = ("Summary\n- Dependency register updated.\n"
                "Work completed\n- Drafted the register.\n"
                "Next actions\n- Jordan: verify the preview checklist.\n"
                "Decisions needed\n- None recorded.\n")
        meta = "The user wants me to act as Morgan.\n" + meta
        self.assertTrue(any("meta-commentary" in item
                             for item in qc_report("Morgan", meta)))

    def test_ceo_waiting_reports_go_to_decision_packet(self):
        chain = HandoffChain("christmas-tumbler-bundles")
        chain.assign("Complete the next deliverable.", "Morgan", "test-ceo")
        waiting = ("Summary\n- Awaiting CEO approval of bundle contents, "
                   "pricing, and inventory.\n"
                   "Work completed\n- Drafted a reusable copy framework.\n"
                   "Next actions\n- Morgan: record the CEO decisions.\n"
                   "Decisions needed\n- Await CEO approval of bundle "
                   "contents, pricing, and inventory.\n")
        reports = {name: waiting for name in
                   ("Morgan", "Avery", "Jordan", "Cameron")}
        self.assertEqual(chain.run_qc(reports), "decision_packet")
        packet = chain.state()["decision_packet"]
        self.assertIn("bundle", packet["approval_needed"].lower())

    def test_decision_packet_shape(self):
        packet = build_decision_packet({"Avery": ["Avery: missing section"]})
        self.assertEqual(len(packet["options"]), 3)
        for key in ("options", "recommendation", "impact", "approval_needed"):
            self.assertTrue(packet[key])


class AutonomyHandoffWiringTests(unittest.TestCase):
    def test_qc_after_cycle_writes_observable_stage(self):
        from staff.report_projects import ensure_project
        from staff.staff_autonomy import StaffAutonomy
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, _, path = ensure_project(root, "nov-1-site-launch",
                                        "Nov 1 Site Launch", activate=True)
            report_dir = path / "agent-reports"
            report_dir.mkdir(parents=True, exist_ok=True)
            for filename in ("morgan-operations.md", "avery-marketing.md",
                             "jordan-web-it.md", "cameron-hr-legal.md"):
                (report_dir / filename).write_text(GOOD)
            manager = StaffAutonomy(root)
            handoff = manager._qc_after_cycle({"handoff": {}})
            self.assertEqual(handoff["stage"], "qc_pass")
            status = manager.root.joinpath(
                "work", "staff-work-status.json")
            self.assertEqual(
                __import__("json").loads(status.read_text())["handoff"]["stage"],
                "qc_pass")

    def test_derive_assignment_prefers_consolidated_next_action(self):
        from staff.report_projects import ensure_project
        from staff.staff_autonomy import StaffAutonomy
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, _, path = ensure_project(root, "nov-1-site-launch",
                                        "Nov 1 Site Launch", activate=True)
            (path / "consolidated-project-report.md").write_text(
                "# Report\n\n## Next actions\n"
                "- Jordan: verify the preview checklist.\n")
            owner, task = StaffAutonomy(root)._derive_assignment()
            self.assertEqual(owner, "Morgan")
            self.assertIn("verify the preview checklist", task)


if __name__ == "__main__":
    unittest.main()
