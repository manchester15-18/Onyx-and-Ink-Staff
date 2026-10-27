"""Jordan-only Vercel Web Design Guidelines audit tools (prompt files, not a
library, not a deployment).

Source: vercel-labs/agent-skills, skills/web-design-guidelines/SKILL.md
(39 lines; it contains no rules itself, it tells the agent to fetch them),
and the rules file it points at:
https://raw.githubusercontent.com/vercel-labs/web-interface-guidelines/main/command.md
License: MIT, verified at install on Oct 27, 2026 — the guidelines repo
carries a LICENSE file (Copyright (c) 2025 Vercel Labs); the agent-skills
repo README states MIT but has no separate LICENSE file in that repo.

What this is: a review-only, preview-only quality gate. Jordan fetches (or
reuses a snapshot of) the guidelines, reads the named storefront files, and
reports terse file:line findings grouped by file. It complements the
Impeccable detector: Impeccable deterministically detects anti-patterns in
code; Vercel adds human-judgment checks (forms, focus order, copy, i18n,
dark mode) a detector cannot fully judge. It does NOT replace Impeccable.

Deliberately NOT wired: vercel-deploy-claimable and every other skill in
the agent-skills repo (react-best-practices, deploy-to-vercel, etc.).
Nothing here creates a deployment, touches Cloudflare Pages settings, or
touches production. Any storefront edit found by an audit still goes
through the preview-branch gate and James's approval.

Snapshot rule (the live-fetch caveat): the guidelines are fetched live by
design and can drift silently between reviews, so Jordan must refresh the
snapshot before each review with refresh_vercel_snapshot() and keep the
SNAPSHOT.txt from that refresh with the review. An audit run against a
stale, undated snapshot is not reproducible and must say so.

Known gaps in the condensed command sheet (do not claim otherwise): it
omits parts of the full guidelines, including minimum hit-target size,
the 16px mobile-input rule, focus management detail, and most of the
visual Design section. Those gaps are why this audit is a supplement to
Impeccable and to Jordan's screenshot audit, not a replacement for either.

Vanilla filter (the storefront is vanilla HTML/CSS/JS, not React): skip
React-only rules outright — useState/nuqs URL sync, <Link>, Tailwind
utility classes (focus-visible:ring-*, hover:*), React props (htmlFor,
spellCheck, onKeyDown, suppressHydrationWarning), and virtua
virtualization. Translate the intent to vanilla equivalents where they
exist (a <label for>, :focus-visible CSS, explicit width/height on <img>),
and never report a React-only rule as a failure of a vanilla page.

Brand guard (applies to every result): the Onyx & Ink storefront brand
system in cloudflare-storefront/JORDAN_SITE_WORKSPACE.md — rich Onyx green,
warm paper neutrals, restrained gold/clay accents, editorial serif moments
with clear sans-serif body text — OVERRIDES any skill palette, font, or
style that conflicts with it. Vercel's rules govern quality, not the Onyx
palette, and its copy rules (Title Case headings, active voice) never
override the storefront's existing, owner-approved copy without a
preview-branch decision.

Files live on the PC at work/vercel-design-guidelines/ (mounted into the
container as /app/work/, git-ignored like other PC work state). They are
NOT committed to the Staff repo; re-copy them there if the PC work/
folder is lost. Skill/guidelines text is treated as data/guidance, never
executed, and never as permission to change a site.
"""
import hashlib
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GUIDELINES_URL = (
    "https://raw.githubusercontent.com/vercel-labs/web-interface-guidelines/main/command.md"
)
MAX_CHARS = 20000
MAX_FILE_CHARS = 12000

BRAND_NOTE = (
    "BRAND + VANILLA GUARD: Onyx & Ink storefront brand "
    "(JORDAN_SITE_WORKSPACE.md) overrides any conflicting skill palette/font/"
    "style: rich Onyx green, warm paper neutrals, restrained gold/clay accents, "
    "editorial serif + clear sans-serif body. Storefront is vanilla HTML/CSS/JS: "
    "skip React-only rules outright (useState/nuqs, <Link>, Tailwind ring/hover "
    "utilities, htmlFor/spellCheck/onKeyDown props, suppressHydrationWarning, "
    "virtua) and translate intent to vanilla equivalents where they exist. "
    "This audit is REVIEW-ONLY and PREVIEW-ONLY: it never edits, never deploys, "
    "and never uses any Vercel deployment skill. Known gaps in the condensed "
    "command sheet: minimum hit-target size, 16px mobile-input rule, focus "
    "management detail, and most of the visual Design section are omitted — "
    "so this audit supplements Impeccable and the screenshot audit, it does "
    "not replace them."
)


def find_vercel_dir(project_dir):
    """Return the Vercel skill-files directory, or None if not installed.

    Only project_dir/work/ is checked: in the container PROJECT_DIR is
    /app, so that already is /app/work/. Checking a hardcoded /app/work/
    as well would make a missing install untestable and would silently
    use another project's files when project_dir points elsewhere.
    """
    root = Path(project_dir) / "work" / "vercel-design-guidelines"
    if (root / "SKILL.md").is_file():
        return root
    return None


def get_vercel_skill(project_dir, include_rules=False):
    """Return the Vercel SKILL.md text, plus the brand/vanilla guard.

    With include_rules=True, appends the snapshotted command.md rules so
    Jordan can audit without a second read. Rules are capped with the
    skill text at MAX_CHARS total.
    """
    root = find_vercel_dir(project_dir)
    if root is None:
        return ("Vercel Web Design Guidelines not found. Copy them on the PC "
                "to work/vercel-design-guidelines/ (SKILL.md from "
                "github.com/vercel-labs/agent-skills skills/web-design-guidelines, "
                "command.md from vercel-labs/web-interface-guidelines, MIT) "
                "and retry. They are not bundled in the Staff repo.")
    skill_path = root / "SKILL.md"
    if not skill_path.is_file():
        return f"Vercel skill is missing its SKILL.md in {root}."
    text = skill_path.read_text(encoding="utf-8", errors="replace")
    snapshot = root / "SNAPSHOT.txt"
    if snapshot.is_file():
        first = snapshot.read_text(encoding="utf-8", errors="replace").splitlines()
        taken = next((ln for ln in first if ln.startswith("Snapshot taken:")), "")
        text += f"\n\nSnapshot on file: {taken or 'undated — refresh before reviewing.'}"
    else:
        text += "\n\nNo SNAPSHOT.txt on file — refresh before reviewing."
    if include_rules:
        rules_path = root / "command.md"
        if rules_path.is_file():
            text += "\n\n--- Snapshotted guidelines: command.md ---\n\n"
            text += rules_path.read_text(encoding="utf-8", errors="replace")
        else:
            text += "\n\nSnapshotted command.md is missing — refresh before reviewing."
    return text[:MAX_CHARS] + "\n\n" + BRAND_NOTE


def refresh_vercel_snapshot(project_dir, url=None):
    """Fetch the guidelines, write command.md + SNAPSHOT.txt, report counts.

    Uses only the standard library (urllib), 30s timeout, no shell. The
    url parameter exists only so tests can point at a local file; Jordan
    always uses the default GUIDELINES_URL. Returns a short status string,
    never the fetched content itself.
    """
    root = find_vercel_dir(project_dir)
    if root is None:
        # Fall back to the conventional work location so a first refresh
        # can create the directory instead of failing on a missing root.
        root = Path(project_dir) / "work" / "vercel-design-guidelines"
        root.mkdir(parents=True, exist_ok=True)
    target_url = url or GUIDELINES_URL
    try:
        with urllib.request.urlopen(target_url, timeout=30) as response:
            data = response.read()
    except Exception as exc:  # network, DNS, HTTP, timeout: report, don't crash
        return f"Vercel snapshot refresh FAILED: {type(exc).__name__}: {exc}. Previous snapshot (if any) is unchanged."
    rules_path = root / "command.md"
    rules_path.write_bytes(data)
    text = data.decode("utf-8", errors="replace")
    bullets = sum(1 for line in text.splitlines() if line.strip().startswith("- "))
    digest = hashlib.sha256(data).hexdigest()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot = (
        "Vercel Web Design Guidelines snapshot\n"
        f"Source: {GUIDELINES_URL}\n"
        "Skill: https://github.com/vercel-labs/agent-skills (skills/web-design-guidelines/SKILL.md)\n"
        "License: MIT (web-interface-guidelines repo LICENSE, Copyright (c) 2025 Vercel Labs)\n"
        f"Snapshot taken: {stamp}\n"
        f"command.md SHA-256: {digest}\n"
        f"command.md size: {len(data)} bytes, {bullets} bullet rules\n"
        "Note: rules are fetched live by design and can drift. Refresh before each review with refresh_vercel_snapshot() and keep the new SNAPSHOT.txt with that review.\n"
    )
    (root / "SNAPSHOT.txt").write_text(snapshot, encoding="utf-8")
    return (f"Vercel snapshot refreshed: {len(data)} bytes, {bullets} bullet "
            f"rules, SHA-256 {digest[:16]}…, taken {stamp}. Keep this SNAPSHOT.txt with the review.")


def _resolve_target(project_dir, target):
    """Resolve a target under storefront/ or work/, or None.

    A bare filename ('index.html') is treated as storefront/index.html,
    matching the Impeccable tool's convention. 'storefront/...' and
    'work/...' prefixes are accepted as written.
    """
    raw = (target or "").strip()
    if not raw or raw.startswith(("http://", "https://", "/", "-")):
        return None
    if "/" not in raw:
        raw = "storefront/" + raw
    base = Path(project_dir)
    candidate = (base / raw).resolve()
    allowed = [(base / "storefront").resolve(), (base / "work").resolve()]
    if not any(candidate == root or root in candidate.parents for root in allowed):
        return None
    return candidate


def prepare_vercel_audit(project_dir, target):
    """Prepare a Vercel audit of one storefront/work file.

    Validates the target, returns the snapshot state, the guard, and the
    first MAX_FILE_CHARS of the file for Jordan to check against the
    snapshotted rules. This tool prepares and reports; Jordan (the agent)
    does the judging and writes the file:line findings. It never edits.
    """
    root = find_vercel_dir(project_dir)
    if root is None:
        return ("Vercel Web Design Guidelines not found. Copy them on the PC "
                "to work/vercel-design-guidelines/ and retry.")
    candidate = _resolve_target(project_dir, target)
    if candidate is None:
        return (f"Vercel audit rejected: target '{target}' is not a relative "
                "path under storefront/ or work/. URLs, absolute paths, flags, "
                "and paths outside those trees are never audited.")
    if not candidate.is_file():
        return f"Vercel audit rejected: target '{target}' does not exist as a file ({candidate})."
    snapshot = root / "SNAPSHOT.txt"
    if snapshot.is_file():
        lines = snapshot.read_text(encoding="utf-8", errors="replace").splitlines()
        taken = next((ln for ln in lines if ln.startswith("Snapshot taken:")), "undated")
        rules = next((ln for ln in lines if "bullet rules" in ln), "")
        snap_line = f"Snapshot: {taken}; {rules}"
    else:
        snap_line = "Snapshot: NO SNAPSHOT.txt — refresh before treating findings as reproducible."
    body = candidate.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_CHARS]
    return (
        f"VERCEL AUDIT PREP: target={candidate}\n{snap_line}\n"
        "Apply the snapshotted command.md rules (get_vercel_skill with rules), "
        "skip React-only rules, report terse file:line findings grouped by file, "
        "and state any finding honestly — never auto-ignore a real finding. "
        "This is review-only: no edit, no deploy.\n\n"
        + BRAND_NOTE + "\n\n--- File content (first "
        + f"{MAX_FILE_CHARS} chars) ---\n\n" + body
    )
