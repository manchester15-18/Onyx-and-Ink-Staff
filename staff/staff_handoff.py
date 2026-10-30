"""Automatic Morgan handoff chain for autonomous staff work.

Pure logic, no model calls, no file writes, no network: the autonomy loop
(staff_autonomy.py) drives this chain by default on every cycle, so Morgan
assigns, an agent delivers, Morgan QC-checks, and corrections or a decision
packet follow without James prompting each step.

Stages, in order:
    assigned -> delivered -> qc_pass
    assigned -> delivered -> qc_retry_1 -> delivered -> qc_retry_2
        -> delivered -> decision_packet
    assigned -> decision_packet  (only when a CEO decision truly blocks)

Retry bound: MAX_QC_RETRIES = 2 corrections. That is 3 deliveries in total
(initial + 2 corrections); a third failed QC produces a decision packet
instead of a fourth delivery, so the chain always terminates.

Constraints preserved by construction:
  - Nothing here publishes to production, merges, emails, buys, or books.
  - No pricing, inventory, delivery promise, policy position, or campaign
    claim is invented: the decision packet is built only from the QC
    failures and the blocker text supplied by the caller, and its options
    are about what to do with the draft, never about business facts.
  - QC is a deterministic structural/claim scan, not a factual verifier:
    it checks that a report has a deliverable section, a next-action /
    next-owner section, and a blocker/decision section, and that it does
    not claim a production publish, a main merge, or an email send as
    completed work. A report that merely *requires* approval (for example
    "merge to main requires James explicit approval") is not flagged.
"""

import re

MAX_QC_RETRIES = 2
AGENTS = ("Morgan", "Avery", "Jordan", "Cameron")
REPORT_FILES = {
    "Morgan": "morgan-operations.md",
    "Avery": "avery-marketing.md",
    "Jordan": "jordan-web-it.md",
    "Cameron": "cameron-hr-legal.md",
}

# A claim of completed production/email work, on one line, that is not
# qualified by a requirement, approval, pending state, or future condition.
_CLAIM_PATTERNS = (
    "published to production",
    "deployed to production",
    "merged to main",
    "sent an email",
    "sent email to",
    "emailed james",
)
_QUALIFIERS = ("requir", "approval", "pending", "after ", "before ", "once ")


def _claim_flagged(line):
    lowered = line.lower()
    if not any(pattern in lowered for pattern in _CLAIM_PATTERNS):
        return False
    return not any(qualifier in lowered for qualifier in _QUALIFIERS)


def _has_section(text, *markers):
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


def qc_report(agent, text):
    """Return specific corrections for one deliverable, [] when it passes.

    A deliverable passes when it is non-trivial, names a deliverable (or a
    summary in the standardised report format), names a next action / next
    owner, names a remaining blocker / open decision, and claims no
    completed production or email work.
    """
    corrections = []
    body = str(text or "").strip()
    if len(body) < 50:
        corrections.append(
            "%s: deliverable is missing or too short to QC — deliver one "
            "usable draft, checklist, copy, decision register, or acceptance "
            "criteria." % agent
        )
        return corrections
    # Substance check (Oct 29, 2026 audit): section headings and
    # "None recorded" placeholders do not count as work. A report that
    # only says "None recorded" under every section passed the old QC
    # because it contained the right keywords.
    substantive = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lowered = stripped.lower()
        if lowered in ("none recorded", "none", "no work completed yet",
                       "no work completed", "not recorded"):
            continue
        if lowered.startswith("- none recorded") or lowered.startswith("- none"):
            continue
        if lowered in ("deliverable", "summary", "work completed",
                       "assumptions and risks", "next actions",
                       "open decision", "decisions needed"):
            continue
        substantive.append(stripped)
    substantive_bullets = [
        line for line in substantive
        if line.startswith("- ") and len(line) > 12
    ]
    if len(substantive_bullets) < 2 and sum(
            len(line) for line in substantive) < 150:
        corrections.append(
            "%s: deliverable is too thin -- sections filled only with "
            "'None recorded' do not count. Deliver actual draft, "
            "checklist, copy, decision register, or acceptance criteria "
            "with real content." % agent
        )
    # Reasoning/meta-commentary must not leak into the saved report.
    head = body[:800].lower()
    if "the user wants me to" in head or "let me analyze" in head:
        corrections.append(
            "%s: remove reasoning/meta-commentary from the report "
            "(e.g. 'The user wants me to...') and lead with the "
            "deliverable itself." % agent
        )
    if not _has_section(body, "deliverable", "summary"):
        corrections.append(
            "%s: add a Deliverable (or Summary) section stating what was "
            "actually produced." % agent
        )
    if not _has_section(body, "next action"):
        corrections.append(
            "%s: add a Next action section naming the next owner and the "
            "next concrete step." % agent
        )
    if not _has_section(body, "open decision", "decisions needed", "blocker"):
        corrections.append(
            "%s: add an Open decision / Decisions needed section naming the "
            "remaining blocker, or state explicitly that none remains." % agent
        )
    for line in body.splitlines():
        if _claim_flagged(line):
            corrections.append(
                "%s: remove the claimed completed action in %r — production "
                "publishes, main merges, and emails are never claimed unless "
                "evidence of them was supplied." % (agent, line.strip()[:120])
            )
            break
    return corrections


def qc_reports(reports):
    """QC every agent report. Returns (failures, blocked).

    failures maps agent -> [corrections]. blocked is True only when a
    report explicitly states that no further useful work is possible
    without a CEO decision and carries no usable deliverable section.
    """
    failures = {}
    blocked = False
    ceo_waiting = 0
    for agent in AGENTS:
        text = (reports or {}).get(agent, "")
        corrections = qc_report(agent, text)
        if corrections:
            failures[agent] = corrections
        lowered = str(text or "").lower()
        if ("no further useful work" in lowered or "cannot proceed" in lowered) \
                and "ceo decision" in lowered \
                and not _has_section(text, "deliverable", "summary"):
            blocked = True
        if ("await ceo approval" in lowered
                or "pending ceo approval" in lowered
                or "awaiting ceo approval" in lowered):
            ceo_waiting += 1
    if ceo_waiting >= 2:
        blocked = True
    return failures, blocked


def build_decision_packet(failures, blocker=""):
    """One concise decision packet, built only from QC failures/blocker.

    options are about the draft itself, never new business facts.
    """
    detail = "; ".join(
        "%s: %s" % (agent, "; ".join(items))
        for agent, items in (failures or {}).items()
    ) or "no usable deliverable was produced"
    if blocker:
        detail = "Blocking decision: %s. %s" % (blocker, detail)
    return {
        "options": [
            "Accept the current drafts as-is, with the QC gaps noted",
            "Hold for CEO direction on the blocker below",
            "Reassign with a narrowed scope and the QC corrections applied",
        ],
        "recommendation": "Hold for CEO direction on the blocker below",
        "impact": (
            "After the bounded QC retries, the affected deliverables remain "
            "drafts and must not be treated as final. QC failures: %s" % detail
        ),
        "approval_needed": (
            "Approve one option above, or provide the missing decision: %s"
            % (blocker or detail)
        ),
    }


class HandoffChain:
    """One assign -> deliver -> QC -> retry / decision-packet chain."""

    def __init__(self, project, overview=""):
        self.project = project
        self.overview = overview
        self.stage = "assigned"
        self.owner = "Morgan"
        self.task = ""
        self.assignment_id = ""
        self.retries = 0
        self.corrections = []
        self.decision_packet = None
        self.delivered = {}

    def assign(self, task, owner="Morgan", assignment_id="", keep_retries=False):
        """Morgan assigns the next concrete task. Starts a new chain
        unless keep_retries is True (a QC correction round)."""
        self.task = str(task or "").strip()
        self.owner = owner or "Morgan"
        self.assignment_id = assignment_id or self.assignment_id
        self.stage = "assigned"
        if not keep_retries:
            self.retries = 0
            self.corrections = []
            self.decision_packet = None
            self.delivered = {}
        return self.state()

    def deliver(self, agent, text):
        self.delivered[agent] = str(text or "")
        self.stage = "delivered"
        return self.state()

    def run_qc(self, reports):
        """QC delivered reports. Returns the new stage."""
        failures, blocked = qc_reports(reports)
        if blocked:
            blocker_lines = []
            for agent in AGENTS:
                for line in str((reports or {}).get(agent, "")).splitlines():
                    lowered = line.strip().lower()
                    if "ceo" in lowered and (
                            "approval" in lowered or "decision" in lowered):
                        clean = line.strip().lstrip("- ").strip()
                        if clean and clean not in blocker_lines:
                            blocker_lines.append(clean)
                    if len(blocker_lines) >= 4:
                        break
                if len(blocker_lines) >= 4:
                    break
            blocker = ("; ".join(blocker_lines)
                       if blocker_lines
                       else "CEO decision blocks further useful work")
            self.decision_packet = build_decision_packet(
                failures, blocker=blocker
            )
            self.stage = "decision_packet"
            return self.stage
        if not failures:
            self.corrections = []
            self.decision_packet = None
            self.stage = "qc_pass"
            return self.stage
        self.corrections = [
            item for items in failures.values() for item in items
        ]
        if self.retries < MAX_QC_RETRIES:
            self.retries += 1
            self.stage = "qc_retry_%d" % self.retries
        else:
            self.decision_packet = build_decision_packet(failures)
            self.stage = "decision_packet"
        return self.stage

    def blocked_by_ceo_decision(self, blocker):
        """A CEO decision truly blocks: one packet, no further retries."""
        failures, _ = qc_reports(self.delivered)
        self.decision_packet = build_decision_packet(failures, blocker=blocker)
        self.stage = "decision_packet"
        return self.stage

    def correction_directive(self):
        """Corrections to prepend to the next cycle's directive, '' if none."""
        if not self.stage.startswith("qc_retry") or not self.corrections:
            return ""
        return (
            "Morgan QC corrections from the previous delivery — address "
            "each one before any new work:\n"
            + "\n".join("- " + item for item in self.corrections)
        )

    def state(self):
        return {
            "project": self.project,
            "stage": self.stage,
            "owner": self.owner,
            "task": self.task,
            "assignment_id": self.assignment_id,
            "retries": self.retries,
            "max_retries": MAX_QC_RETRIES,
            "corrections": list(self.corrections),
            "decision_packet": self.decision_packet,
            "agents_delivered": sorted(self.delivered),
        }
