"""Avery's open-ended design learning loop (Oct 27, 2026).

How it works, honestly:
1. Generate a design (text-free, product-locked) via design_images.generate_text_free().
   That function now automatically appends LEARNED LESSONS for that product
   from work/design-lessons.json, so every generation benefits from every
   previous critique — no one has to remember to apply them.
2. Critique the design with staff.design_learning.critique_design().
   Code cannot reliably "see" a product or read AI text from pixels, so
   critique is structured + deterministic (file, format, size, DPI, prompt
   guardrail presence) PLUS observations from whoever looked at the image
   (Avery, via her CrewAI critique tool, or a human). Those observations are
   the critique — the script does not pretend to vision-check the image.
3. Every critique is appended to work/design-critiques.jsonl (the audit trail),
   and every suggested fix becomes a lesson in work/design-lessons.json for
   that product (and, for text leaks, a global lesson). The next generation
   for that product automatically includes those lessons.
4. The loop is open-ended: there is no final prompt. Lessons accumulate,
   are capped (5 per product, 3 global, most recent first) to avoid prompt
   bloat, and are deduplicated. A lesson that stops working can be removed
   by editing design-lessons.json — no code change needed.

CrewAI Avery gets two new tools in main.py: "Critique a design" and
"Generate a design with learned lessons" (the latter is generate_text_free,
which already applies lessons automatically).
"""
import json
from datetime import datetime, timezone
from pathlib import Path

MAX_PRODUCT_LESSONS = 5
MAX_GLOBAL_LESSONS = 3
GLOBAL_TEXT_LESSON = (
    "no text on packaging, labels, neck labels, cards, or price tags — "
    "text leaks happen there even with a no-text guardrail; check the image before publishing"
)


def _lessons_path(root):
    return Path(root) / "work" / "design-lessons.json"


def _critiques_path(root):
    return Path(root) / "work" / "design-critiques.jsonl"


def load_lessons(root):
    path = _lessons_path(root)
    if not path.exists():
        return {"global": [], "products": {}, "agents": {}}
    try:
        data = json.loads(path.read_text())
        data.setdefault("global", [])
        data.setdefault("products", {})
        data.setdefault("agents", {})
        return data
    except Exception:
        return {"global": [], "products": {}, "agents": {}}


def _save_lessons(root, data):
    path = _lessons_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def add_lesson(root, product, lesson, scope="product"):
    """Add a lesson, deduplicated, most-recent-first, capped."""
    lesson = (lesson or "").strip()
    if not lesson:
        return False
    data = load_lessons(root)
    if scope == "global":
        lessons = data["global"]
        cap = MAX_GLOBAL_LESSONS
    elif scope == "agent":
        key = str(product).lower()
        lessons = data["agents"].setdefault(key, [])
        cap = MAX_PRODUCT_LESSONS
    else:
        key = str(product).lower()
        lessons = data["products"].setdefault(key, [])
        cap = MAX_PRODUCT_LESSONS
    if lesson.lower() in (x.lower() for x in lessons):
        return False
    lessons.insert(0, lesson)
    del lessons[cap:]
    _save_lessons(root, data)
    return True


def learned_lessons_for(root, product, agent=None):
    data = load_lessons(root)
    lessons = list(data.get("global", []))
    lessons.extend(data.get("products", {}).get(str(product).lower(), []))
    if agent:
        lessons.extend(data.get("agents", {}).get(str(agent).lower(), []))
    return lessons


def learned_suffix(root, product, agent=None):
    lessons = learned_lessons_for(root, product, agent=agent)
    if not lessons:
        return ""
    return ", learned from past critiques: " + "; ".join(lessons)


def full_learned_prompt(root, prompt, product="tumbler", agent=None):
    """Base text-free prompt + learned lessons + reference notation, for logging / display."""
    from staff.design_tools import build_text_free_prompt
    base = build_text_free_prompt(prompt, product=product)
    out = base + learned_suffix(root, product, agent=agent)
    try:
        from staff.design_approval import reference_notation
        notation = reference_notation(root, product)
        if notation:
            out += "," + notation
    except Exception:
        pass
    return out


def critique_design(root, image_path, product, verdict, what_was_right, what_was_wrong, suggested_fix="", text_found=False, agent="Avery"):
    """Record a critique and turn it into lessons for the next generation.

    verdict: "pass", "partial", or "fail".
    what_was_right / what_was_wrong: plain-language observations from looking
    at the image (Avery or a human). suggested_fix: the prompt change that
    should prevent the wrong part next time — this becomes a product lesson.
    text_found: True if any AI text appeared anywhere in the image.
    Returns a dict with deterministic checks + the lessons added.
    """
    from PIL import Image
    verdict = str(verdict).lower()
    if verdict not in ("pass", "partial", "fail"):
        raise ValueError("verdict must be pass, partial, or fail")
    src = Path(image_path)
    checks = {}
    if not src.exists():
        checks["file_exists"] = False
        checks["error"] = f"File not found: {src}"
    else:
        checks["file_exists"] = True
        checks["format"] = src.suffix.lower()
        try:
            with Image.open(src) as im:
                checks["size"] = f"{im.width}x{im.height}"
                checks["mockup_resolution_ok"] = im.width >= 1024 and im.height >= 1024
        except Exception as exc:
            checks["readable"] = False
            checks["error"] = str(exc)
    lessons_added = []
    if suggested_fix and suggested_fix.strip():
        if add_lesson(root, product, suggested_fix.strip(), scope="product"):
            lessons_added.append(f"product lesson: {suggested_fix.strip()}")
    if text_found:
        if add_lesson(root, product, GLOBAL_TEXT_LESSON, scope="global"):
            lessons_added.append(f"global lesson: {GLOBAL_TEXT_LESSON}")
    record = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "image": str(src),
        "agent": agent,
        "product": str(product),
        "verdict": verdict,
        "what_was_right": what_was_right,
        "what_was_wrong": what_was_wrong,
        "suggested_fix": suggested_fix,
        "text_found": bool(text_found),
        "checks": checks,
        "lessons_added": lessons_added,
    }
    path = _critiques_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return record


def infer_product(prompt):
    """Infer a known product from a free-text prompt, or None.

    Longest product name first, so "snow globe tumbler" wins over "tumbler".
    Used by dashboard design: known products get learned lessons, generic
    designs keep the original prompt (no wrong product lock).
    """
    from staff.design_tools import PRODUCT_LOCKS
    lowered = (prompt or "").lower()
    for name in sorted(PRODUCT_LOCKS, key=len, reverse=True):
        if name in lowered:
            return name
    return None
