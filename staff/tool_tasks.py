"""Deterministic tool execution inside the autonomy loop.

Autonomous cycles are text-only by design: an agent's report can request
a tool, but it cannot run one. This module closes that gap without
handing the model a shell. On every loop tick the dashboard drains:

1. Queued tool tasks — staff tasks whose payload carries a ``tool``
   dict (``action``, ``args``, ``ceo_approved``), created through
   ``staff_tasks.enqueue(..., tool=...)``.
2. ``TOOL:`` bullets in the active project's consolidated report
   Next-actions section, e.g. ``- TOOL: cloudflare_d1_list {}``.

Each request runs through the existing ``Actions.dispatch`` allowlist
as Jordan (the agent that owns infrastructure and Square), never as an
arbitrary shell. Publishing actions — anything that writes Cloudflare
infrastructure, binds a database, sets Square stock, or publishes a
product — require ``ceo_approved``; without it the request is recorded
as skipped, never executed, so the next cycle's report can raise it as
a decision item instead of silently dropping it. Two actions gate
themselves instead: product_create and square_link_catalog downgrade
publish mode to stage-only without approval, so the patch is staged
but nothing reaches Square.

Every outcome is appended to ``work/tool-task-results.json`` and
surfaced to the next cycle through ``tool_results_context`` as supplied
evidence. A cycle may report these results; it must never claim a
change that is not listed there. Failed tasks are completed with their
error recorded — there is no silent retry loop, so a bad request cannot
spin forever.
"""

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("America/New_York")

# Actions the loop may execute on its own. ``product_create`` is special:
# the pipeline itself downgrades publish mode to stage-only when the
# request is not CEO-approved, so the executor never skips it outright.
TOOL_ALLOWLIST = {
    "cloudflare_d1_list",
    "cloudflare_d1_query",
    "cloudflare_pages_project",
    "cloudflare_d1_create",
    "cloudflare_pages_bind_d1",
    "square_inventory_set",
    "product_create",
    "square_link_catalog",
}

# Actions that must never run without CEO approval.
_APPROVAL_ACTIONS = {
    "cloudflare_d1_create",
    "cloudflare_pages_bind_d1",
    "square_inventory_set",
}

TOOL_BULLET_RE = re.compile(r"^-\s*TOOL:\s*([a-z0-9_]+)(?:\s+(\{.*\}))?\s*$")

_SECRET_KEY_ENDINGS = ("_token", "_secret", "_key", "_password")
_SECRET_KEYS = {"token", "secret", "password", "api_key", "apikey", "auth", "authorization"}


def _clean(value):
    """Strip credential-looking values from anything recorded."""
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if str(key).lower() in _SECRET_KEYS
                    or str(key).lower().endswith(_SECRET_KEY_ENDINGS) else _clean(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _results_path(root):
    return Path(root) / "work" / "tool-task-results.json"


def _load_results(root):
    try:
        data = json.loads(_results_path(root).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            return data["results"]
    except (OSError, ValueError, TypeError):
        pass
    return []


def _save_results(root, results):
    path = _results_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"results": results[-200:]}, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _record(root, results, record):
    record["at"] = datetime.now(BUSINESS_TZ).isoformat(timespec="seconds")
    results.append(record)
    _save_results(root, results)
    return record


def _summarize(action, result):
    if not isinstance(result, dict):
        return str(result)[:200]
    if "error" in result:
        return "error: " + str(result["error"])[:180]
    for key in ("summary", "result", "status"):
        if result.get(key):
            return str(result[key])[:200]
    return json.dumps(result, default=str)[:200]


def parse_tool_bullet(line):
    """Parse a Next-actions TOOL bullet.

    Returns (action, args, error): exactly one of args/error is set.
    A non-TOOL line returns (None, None, None).
    """
    match = TOOL_BULLET_RE.match((line or "").strip())
    if not match:
        return None, None, None
    action = match.group(1)
    raw = match.group(2)
    if raw is None:
        return action, {}, None
    try:
        args = json.loads(raw)
    except ValueError:
        return action, None, "TOOL arguments are not valid JSON."
    if not isinstance(args, dict):
        return action, None, "TOOL arguments must be a JSON object."
    return action, args, None


def _needs_approval(action, args):
    if action in _APPROVAL_ACTIONS:
        return True
    if action == "cloudflare_d1_query":
        # SELECT is a read; anything else the module allows (CREATE
        # TABLE / CREATE INDEX) is a write and needs approval.
        sql = str((args or {}).get("sql", "")).strip().upper()
        return not sql.startswith("SELECT")
    return False


def _execute(root, action, args, run_id, executor):
    if executor is not None:
        return executor(action, args, run_id)
    from staff.agent_actions import Actions
    return Actions(root, "Jordan", run_id).execute(action, args)


def _run(root, results, action, args, ceo_approved, run_id, source, task_id, executor):
    """Execute one tool request and record the outcome. Never raises."""
    if action not in TOOL_ALLOWLIST:
        record = {"source": source, "task_id": task_id, "action": action,
                  "args": _clean(args), "ceo_approved": bool(ceo_approved),
                  "status": "rejected", "summary": "Unknown or disallowed tool action: " + str(action)}
        return _record(root, results, record)
    if _needs_approval(action, args) and not ceo_approved:
        record = {"source": source, "task_id": task_id, "action": action,
                  "args": _clean(args), "ceo_approved": False,
                  "status": "skipped",
                  "summary": "Skipped: %s needs CEO approval; no change was made." % action}
        return _record(root, results, record)
    if action == "product_create" or action == "square_link_catalog":
        args = dict(args or {})
        args["ceo_approved"] = bool(ceo_approved)
    try:
        result = _execute(root, action, args, run_id, executor)
    except Exception as error:  # defensive: dispatch normally returns errors
        result = {"error": "%s: %s" % (type(error).__name__, str(error)[:200])}
    status = "failed" if isinstance(result, dict) and "error" in result else "complete"
    record = {"source": source, "task_id": task_id, "action": action,
              "args": _clean(args), "ceo_approved": bool(ceo_approved),
              "status": status, "summary": _summarize(action, result),
              "result": _clean(result)}
    return _record(root, results, record)


def drain_tool_tasks(root, project_id, project_folder=None, executor=None, limit=5):
    """Execute pending tool tasks and unseen TOOL bullets for a project.

    Bounded (``limit`` executions per tick) and failure-tolerant: every
    outcome is recorded and the task is completed, so nothing retries
    silently. Returns the list of records created on this drain.
    """
    root = Path(root)
    results = _load_results(root)
    created = []
    try:
        from staff.staff_tasks import status as task_status, complete as task_complete
    except ImportError:
        from staff_tasks import status as task_status, complete as task_complete

    # 1. Queued tool tasks, oldest first (status() returns newest first).
    try:
        queued = [item for item in task_status(root, 100)
                  if item.get("project") == project_id and item.get("status") == "pending"
                  and isinstance(item.get("tool"), dict)]
    except (OSError, ValueError, TypeError):
        queued = []
    for task in reversed(queued):
        if len(created) >= limit:
            break
        tool = task["tool"]
        action = str(tool.get("action", "")).strip()
        args = tool.get("args") if isinstance(tool.get("args"), dict) else {}
        approved = bool(tool.get("ceo_approved"))
        if not action:
            record = {"source": "queue", "task_id": task.get("id"), "action": "",
                      "args": {}, "ceo_approved": approved, "status": "rejected",
                      "summary": "Tool task has no action."}
            created.append(_record(root, results, record))
        else:
            created.append(_run(root, results, action, args, approved,
                                "tool-task-" + str(task.get("id", "")), "queue",
                                task.get("id"), executor))
        try:
            task_complete(root, task.get("id"), created[-1]["summary"])
        except (OSError, ValueError, TypeError):
            pass

    # 2. TOOL bullets in the consolidated report's Next-actions section.
    if len(created) < limit:
        folder = project_folder
        if folder is None:
            try:
                from staff.report_projects import active_project
            except ImportError:
                from report_projects import active_project
            try:
                folder = active_project(root)[2]
            except (OSError, ValueError):
                folder = None
        if folder is not None:
            try:
                lines = (Path(folder) / "consolidated-project-report.md").read_text(
                    encoding="utf-8").splitlines()
            except OSError:
                lines = []
            seen = {item.get("bullet_hash") for item in results if item.get("source") == "bullet"}
            in_next = False
            for line in lines:
                if len(created) >= limit:
                    break
                lowered = line.lower()
                if "next action" in lowered:
                    in_next = True
                    continue
                if in_next:
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        break
                    action, args, error = parse_tool_bullet(stripped)
                    if action is None:
                        continue
                    digest = hashlib.sha256(stripped.encode()).hexdigest()
                    if digest in seen:
                        continue
                    seen.add(digest)
                    if error is not None:
                        record = {"source": "bullet", "bullet_hash": digest, "action": action,
                                  "args": {}, "ceo_approved": False, "status": "rejected",
                                  "summary": error}
                        created.append(_record(root, results, record))
                        continue
                    record = _run(root, results, action, args, False,
                                  "tool-bullet-" + digest[:16], "bullet", None, executor)
                    record["bullet_hash"] = digest
                    _save_results(root, results)
                    created.append(record)
    return created


def tool_results_context(root, limit=5):
    """Compact evidence section for the next cycle's directive."""
    results = _load_results(root)
    if not results:
        return ""
    lines = ["Tool execution results (supplied evidence — report only what is listed here, never claim an unlisted change):"]
    for record in results[-limit:]:
        lines.append("- [%s] %s: %s" % (record.get("status", "?"), record.get("action", "?"),
                                        str(record.get("summary", ""))[:200]))
    return "\n".join(lines)
