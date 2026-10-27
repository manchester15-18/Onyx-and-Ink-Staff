"""Jordan-only Impeccable detector tool (read-only quality gate).

Impeccable is already present in the storefront: impeccable ^4.7.0 in
cloudflare-storefront/package.json, script "design:check": "impeccable detect .",
and a skill copy at cloudflare-storefront/.agents/skills/impeccable/ (its
launcher is a self-contained binary, so the detector itself needs no Node).
In the dashboard container the detector is installed globally via npm
(see Dockerfile) once Node is present, and the storefront is mounted
read-only at /app/storefront.

Rules for this tool, enforced in code:
- DETECT ONLY. It never edits a file, never uses Impeccable live mode, never
  injects anything into a deployed site, and never runs the skill's
  generative commands (shape, craft, polish, etc.) — those are Jordan's
  separate decisions, not this tool.
- File/dir targets only, inside /app/storefront (the preview-branch checkout
  on the PC) or /app/work (sample files). URLs, CDP targets, absolute paths
  outside those roots, and parent-directory escapes are rejected.
- Output is data: a summary (total, counts by antipattern, first 20 findings
  with file/antipattern/snippet) plus the detector exit code — 0 = no primary
  findings, 2 = primary findings, 1 = scan failure. Advisory findings are
  listed separately by the detector and never counted as failures.
- Findings are reported, not auto-ignored. A brand-deliberate rule (e.g.
  cream-palette for the documented warm paper background) may only be waived
  through the storefront's .impeccable/config.json with a stated reason, or
  a DESIGN.md entry — never by this tool silently dropping a rule. Real
  quality findings (low contrast, undersized UI text, cramped padding) are
  never ignored by default.
"""
import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path

ALLOWED_ROOTS = (Path("/app/storefront"), Path("/app/work"))
MAX_FINDINGS_SHOWN = 20
MAX_SNIPPET_CHARS = 300


def find_detector():
    """Return the detector executable path, or None."""
    found = shutil.which("impeccable")
    if found:
        return found
    for candidate in (
        Path("/app/storefront/node_modules/.bin/impeccable"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _resolve_target(project_dir, target):
    """Resolve a target to an allowed path, or return an error string."""
    raw = (target or "").strip()
    if not raw:
        return None, "Impeccable detect rejected: target is empty."
    if raw.startswith(("http://", "https://", "file://", "--")):
        return None, ("Impeccable detect rejected: URL and flag targets are "
                       "not allowed here; use a file or directory inside the "
                       "storefront (preview checkout) or work/ instead.")
    path = Path(raw)
    if path.is_absolute():
        resolved = path.resolve()
    else:
        # Relative targets are resolved against the storefront root first,
        # then work/ — a target must exist under one of the allowed roots.
        for root in ALLOWED_ROOTS:
            candidate = (root / raw).resolve()
            if candidate.exists():
                resolved = candidate
                break
        else:
            return None, (f"Impeccable detect rejected: '{raw}' does not exist "
                          "under /app/storefront or /app/work.")
    for root in ALLOWED_ROOTS:
        if resolved == root or root in resolved.parents:
            return resolved, None
    return None, ("Impeccable detect rejected: target is outside the allowed "
                  "roots (/app/storefront, /app/work).")


def detect_impeccable(project_dir, target, scope=None):
    """Run the Impeccable detector (read-only) on one file or directory."""
    detector = find_detector()
    if detector is None:
        return ("Impeccable detector not found in this container. It is "
                "installed globally via npm in the dashboard image (see "
                "Dockerfile); rebuild the dashboard image and retry. On the "
                "PC host, the storefront's own binary also works: "
                "cloudflare-storefront/.agents/skills/impeccable/scripts/"
                "impeccable.cmd detect --json <target>.")
    resolved, error = _resolve_target(project_dir, target)
    if error:
        return error
    cmd = [detector, "detect", "--json"]
    if scope:
        allowed_scopes = {"type", "layout"}
        parts = [s.strip() for s in str(scope).split(",") if s.strip() in allowed_scopes]
        if parts:
            cmd += ["--scope", ",".join(parts)]
    cmd.append(str(resolved))
    cwd = "/app/storefront" if Path("/app/storefront") in resolved.parents or resolved == Path("/app/storefront") else "/app/work"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, cwd=cwd)
    except subprocess.TimeoutExpired:
        return "Impeccable detect timed out after 180s."
    except Exception as exc:  # noqa: BLE001 - surface the failure
        return f"Impeccable detect failed: {type(exc).__name__}: {exc}"
    try:
        findings = json.loads(proc.stdout) if proc.stdout.strip() else []
    except json.JSONDecodeError:
        return (f"Impeccable detect returned exit {proc.returncode} but its "
                f"output was not JSON. stdout: {(proc.stdout or '')[:500]} "
                f"stderr: {(proc.stderr or '')[:500]}")
    if not isinstance(findings, list):
        return f"Impeccable detect returned an unexpected JSON shape: {str(findings)[:500]}"
    counts = Counter(f.get("antipattern", "?") for f in findings)
    lines = [
        f"IMPECCABLE DETECT: target={resolved} exit={proc.returncode} "
        f"(0=no primary findings, 2=primary findings, 1=scan failure) "
        f"total={len(findings)}",
        "Counts by rule: " + (", ".join(f"{k}={v}" for k, v in counts.most_common()) or "none"),
        "Findings are reported, not auto-ignored. Brand-deliberate waivers "
        "belong in the storefront .impeccable/config.json with a stated reason.",
    ]
    for f in findings[:MAX_FINDINGS_SHOWN]:
        snippet = str(f.get("snippet", ""))[:MAX_SNIPPET_CHARS]
        lines.append(f"- {f.get('file', '?')} | {f.get('antipattern', '?')} | {snippet}")
    if len(findings) > MAX_FINDINGS_SHOWN:
        lines.append(f"... and {len(findings) - MAX_FINDINGS_SHOWN} more findings (full JSON was parsed, only the first {MAX_FINDINGS_SHOWN} are shown).")
    return "\n".join(lines)
