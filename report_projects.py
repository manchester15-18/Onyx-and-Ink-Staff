"""Safe local project folders for staff reports."""
import json
import re
from pathlib import Path

DEFAULT_PROJECT = "custom-gift-push"
DEFAULT_LABEL = "Custom Gift Push"


def project_slug(value):
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    if not text or len(text) > 64:
        raise ValueError("Enter a project name using 64 characters or fewer.")
    return text


def _state_path(root):
    return Path(root) / "work" / "report-projects.json"


def _load(root):
    base = {"active": DEFAULT_PROJECT, "labels": {DEFAULT_PROJECT: DEFAULT_LABEL}}
    try:
        saved = json.loads(_state_path(root).read_text())
        if isinstance(saved, dict):
            base["active"] = project_slug(saved.get("active", DEFAULT_PROJECT))
            labels = saved.get("labels", {})
            if isinstance(labels, dict):
                for key, label in labels.items():
                    slug = project_slug(key)
                    if isinstance(label, str) and label.strip():
                        base["labels"][slug] = label.strip()[:80]
    except (OSError, ValueError, TypeError):
        pass
    return base


def _save(root, state):
    path = _state_path(root)
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def ensure_project(root, name=None, label=None, activate=False):
    state = _load(root)
    slug = project_slug(name or state["active"])
    display = str(label or state["labels"].get(slug) or str(name or slug).replace("-", " ").title()).strip()[:80]
    state["labels"][slug] = display
    if activate:
        state["active"] = slug
    folder = Path(root) / "reports" / slug
    folder.mkdir(parents=True, exist_ok=True)
    _save(root, state)
    return slug, display, folder


def active_project(root):
    state = _load(root)
    return ensure_project(root, state["active"])


def projects(root):
    state = _load(root)
    report_root = Path(root) / "reports"
    report_root.mkdir(exist_ok=True)
    slugs = set(state["labels"])
    slugs.update(path.name for path in report_root.iterdir() if path.is_dir() and not path.name.startswith("."))
    active = state["active"]
    return [
        {"id": slug, "name": state["labels"].get(slug, slug.replace("-", " ").title()), "active": slug == active}
        for slug in sorted(slugs, key=lambda value: (value != active, state["labels"].get(value, value).lower()))
    ]


def migrate_legacy_reports(root, project=DEFAULT_PROJECT):
    """Move the original flat report files into one project without overwriting."""
    _, _, destination = ensure_project(root, project, DEFAULT_LABEL, activate=True)
    for source in (Path(root) / "reports").glob("*.md"):
        target = destination / source.name
        if target.exists():
            target = destination / (source.stem + "-legacy" + source.suffix)
        source.replace(target)
    return destination
