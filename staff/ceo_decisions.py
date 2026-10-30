"""CEO/VP decision intake for blocked autonomy chains (Oct 30, 2026).

Problem it fixes: a decision packet stopped the autonomy loop at stage
``decision_packet``, but a CEO/VP reply to that packet only ever produced
a conversational chat turn. Nothing recorded the decision, nothing
changed the blocked chain, and nothing queued the follow-up work — so
the loop stayed stopped and the reply looked ignored.

This module is the execution path, not a chat path:

* ``parse_decision_text`` matches a reply against the packet's own
  options (accept / hold / reassign) — it never invents an option.
* ``apply_decision`` records the decision in ``work/ceo-decisions.json``
  and mutates the blocked chain in exactly one of three auditable ways:
    - accept: chain closes as ``qc_pass`` (continuous mode then moves
      on to the next concrete launch action);
    - hold: chain stays at ``decision_packet`` on purpose;
    - reassign: one narrowed task per QC correction is queued for the
      named agent (which wakes the loop via ``runNowPending``) and the
      chain resets to ``qc_retry_1`` with its corrections preserved, so
      the next cycle's directive carries them.
* ``handle_decision_email`` is the inbox-monitor entry point. It only
  fires for CEO/VP senders replying to a ``CEO approval needed`` thread
  whose blocked packet is still open, and returns a confirmation text
  for the monitor to send back. Anything else returns ``None`` so the
  monitor's existing reply paths run unchanged.

Decision authority: CEO = the Owner address, VP = Jaunee (Vice
President). Any other sender can never resolve a packet through here.
Idempotency: applying the same assignment twice returns
``already_applied`` and queues nothing twice.
"""

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("America/New_York")
STAFF = ("Morgan", "Avery", "Jordan", "Cameron")

_QUOTE_MARKERS = (
    r"^On .+ wrote:$",
    r"^-{2,}\s*Original Message\s*-{2,}$",
    r"^From:\s+.+",
    r"^>",
    r"^Sent from ",
)


def extract_newest_reply(body):
    """Newest reply text only, with quoted thread context stripped."""
    lines = str(body or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()
    split = len(lines)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if any(re.match(pattern, stripped, re.I) for pattern in _QUOTE_MARKERS):
            split = index
            break
    return "\n".join(lines[:split]).strip()


def extract_assignment_id(body):
    match = re.search(r"Assignment:\s*([0-9A-Za-z][0-9A-Za-z\-]*)", str(body or ""))
    return match.group(1) if match else ""


def is_decision_thread(subject):
    return "ceo approval needed" in str(subject or "").lower()


def parse_decision_text(text):
    """Match reply text to one of the packet's three options.

    Order matters: 'reassign' is checked first because a reassign reply
    never contains the accept/hold keywords, while a bare 'proceed'
    with no option keyword is ambiguous and returns None (the caller
    then asks one focused question instead of guessing).
    """
    lowered = re.sub(r"\s+", " ", str(text or "").lower())
    if "reassign" in lowered or "narrowed scope" in lowered or "narrowed" in lowered:
        return "reassign"
    if "hold" in lowered:
        return "hold"
    if "accept" in lowered:
        return "accept"
    return None


def _load_json(path, default):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except (OSError, ValueError, TypeError):
        return default


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(path)


def _project_label(root, project):
    state = _load_json(Path(root) / "work" / "report-projects.json", {})
    labels = state.get("labels") or {}
    return labels.get(project) or project.replace("-", " ").title()


def _correction_agent(correction):
    """Agent named at the head of a QC correction, else Morgan."""
    head = str(correction or "").split(":", 1)[0].strip()
    return head if head in STAFF else "Morgan"


def apply_decision(root, project, assignment_id, decision, source="chat",
                    sender_role="CEO", sender="James"):
    """Apply a CEO/VP decision to a blocked chain. Pure state + tasks.

    Returns a dict with ``applied`` (bool), a ``confirmation`` string,
    and, for reassign, the ``queued`` task list. Never sends email
    itself — the caller owns notification (monitor reply, chat report).
    """
    root = Path(root)
    if decision not in ("accept", "hold", "reassign"):
        return {"applied": False, "reason": "unknown decision: %s" % decision}
    if sender_role not in ("CEO", "VP"):
        return {"applied": False, "reason": "sender is not a decision authority"}
    autonomy_path = root / "work" / "staff-autonomy.json"
    state = _load_json(autonomy_path, {})
    handoff = state.get("handoff") or {}
    decisions_path = root / "work" / "ceo-decisions.json"
    decisions_data = _load_json(decisions_path, {})
    records = decisions_data.get("decisions") or []
    # Idempotency first: a decision already applied stays applied even
    # after the chain has moved on from decision_packet.
    for record in records:
        if (record.get("assignment_id") == handoff.get("assignment_id")
                and record.get("applied") is True):
            return {"applied": False, "reason": "already_applied",
                    "confirmation": "That decision was already applied; nothing was queued twice."}
    if str(handoff.get("stage", "")) != "decision_packet":
        return {"applied": False, "reason": "no blocked decision packet for this project"}
    if assignment_id and str(handoff.get("assignment_id", "")) != assignment_id:
        return {"applied": False, "reason": "assignment does not match the blocked packet"}

    label = _project_label(root, project or handoff.get("project", ""))
    now = datetime.now(BUSINESS_TZ).isoformat(timespec="seconds")
    queued = []
    if decision == "hold":
        handoff["decision_resolved"] = "hold"
        handoff["decision_applied_at"] = now
        detail = "CEO/VP decision applied: hold. The chain stays stopped at the decision packet."
        status = "waiting"
        confirmation = ("Held, as decided. The %s work stays stopped at the decision "
                        "packet until a new decision arrives." % label)
    elif decision == "accept":
        handoff["stage"] = "qc_pass"
        handoff["retries"] = 0
        handoff["corrections"] = []
        handoff["decision_resolved"] = "accept"
        handoff["decision_applied_at"] = now
        detail = "CEO/VP decision applied: drafts accepted as-is, with QC gaps noted. Chain closed."
        status = "complete"
        confirmation = ("Accepted as-is, with the QC gaps noted. The %s chain is closed; "
                        "continuous mode moves on to the next concrete action." % label)
    else:  # reassign
        from staff.staff_tasks import enqueue
        corrections = list(handoff.get("corrections") or [])
        for correction in corrections:
            agent = _correction_agent(correction)
            task = enqueue(
                root, "CEO", agent,
                "%s Apply this QC correction to your %s deliverable: lead with "
                "the deliverable itself and replace placeholder content with "
                "real content." % (correction, label),
                project or handoff.get("project", ""))
            queued.append({"to": agent, "id": task.get("id"), "text": task.get("text")})
        handoff["stage"] = "qc_retry_1"
        handoff["retries"] = 1
        handoff["decision_resolved"] = "reassign"
        handoff["decision_applied_at"] = now
        detail = ("CEO/VP decision applied: reassigned with narrowed scope. "
                  "QC corrections queued for %s."
                  % ", ".join(sorted({q["to"] for q in queued})) if queued
                  else "CEO/VP decision applied: reassigned with narrowed scope.")
        status = "working"
        confirmation = ("Reassigned with a narrowed scope and the QC corrections applied. "
                        "%d correction task(s) queued (%s); the loop is woken and "
                        "the next cycle carries the corrections."
                        % (len(queued), ", ".join(sorted({q["to"] for q in queued}))))

    # Re-read before writing: enqueue() wakes the loop by setting
    # runNowPending on disk, and writing back the pre-queue snapshot
    # would clobber that wake. The fresh read keeps it.
    fresh = _load_json(autonomy_path, state)
    fresh["handoff"] = handoff
    if decision == "reassign":
        fresh["runNowPending"] = True
    _write_json(autonomy_path, fresh)

    status_path = root / "work" / "staff-work-status.json"
    status_data = _load_json(status_path, {})
    status_data["status"] = status
    status_data["detail"] = detail
    status_data["handoff"] = handoff
    status_data["updated"] = now
    _write_json(status_path, status_data)

    records.append({
        "assignment_id": handoff.get("assignment_id", ""),
        "project": handoff.get("project", ""),
        "decision": decision,
        "source": source,
        "sender_role": sender_role,
        "sender": sender,
        "applied": True,
        "applied_at": now,
        "queued": queued,
    })
    decisions_data["decisions"] = records
    _write_json(decisions_path, decisions_data)
    return {"applied": True, "decision": decision, "queued": queued,
            "confirmation": confirmation}


def _append_chat_transcript(root, agent, user_text, assistant_text):
    """Audit trail in agent-chat, same table the chat path writes."""
    try:
        db = sqlite3.connect(str(Path(root) / "work" / "agent-chat.sqlite3"))
        db.execute("INSERT INTO messages(agent,role,body,source) VALUES (?,?,?,?)",
                   (agent, "user", user_text, "email"))
        db.execute("INSERT INTO messages(agent,role,body,source) VALUES (?,?,?,?)",
                   (agent, "assistant", assistant_text, "email"))
        db.commit()
        db.close()
    except Exception:
        pass


def handle_decision_email(root, mail, message, body, sender_role):
    """Inbox-monitor entry point. Returns confirmation text or None.

    Returns None unless ALL of these hold: sender is CEO/VP, subject is
    a decision-packet thread, the reply names one of the packet options,
    and a packet for that assignment is still blocked. Anything else
    falls through to the monitor's existing reply paths unchanged.
    """
    try:
        if sender_role not in ("CEO", "VP"):
            return None
        subject = str(message.get("Subject", ""))
        if not is_decision_thread(subject):
            return None
        newest = extract_newest_reply(body)
        decision = parse_decision_text(newest)
        if decision is None:
            return None
        state = _load_json(Path(root) / "work" / "staff-autonomy.json", {})
        handoff = state.get("handoff") or {}
        if str(handoff.get("stage", "")) != "decision_packet":
            return None
        assignment = extract_assignment_id(body) or str(handoff.get("assignment_id", ""))
        result = apply_decision(root, handoff.get("project", ""), assignment,
                                decision, source="email", sender_role=sender_role,
                                sender=sender_role)
        if not result.get("applied"):
            return None
        confirmation = result["confirmation"]
        _append_chat_transcript(
            root, "Morgan",
            "CEO/VP decision received by email: %s" % decision,
            confirmation)
        return confirmation
    except Exception:
        return None
