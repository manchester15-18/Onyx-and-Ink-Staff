"""Jordan-only Emil Kowalski design-skill tools (prompt files, not a library).

Source: emilkowalski/skills, MIT License (Copyright (c) 2026 Emil Kowalski),
verified at install on Oct 27, 2026. Only three skills are wired, because
only they fit a vanilla HTML/CSS/JS storefront:

- animate: build an animation from scratch. Vanilla-first tool order:
  CSS transition, @starting-style, CSS animation, Web Animations API, then
  Motion as a last resort. Animate transform and opacity only, UI motion
  under 300ms, never ease-in on a UI entrance, reduced-motion and hover
  gating ship with the animation, extend the codebase's existing tokens
  instead of forking them. The storefront already has --ease:
  cubic-bezier(.2, .75, .25, 1) in styles.css — use it, do not invent a
  parallel easing system.
- review-animations: strict review of existing motion against STANDARDS.md.
  Maps directly onto Jordan's existing screenshot/audit flow: look first,
  then judge.
- improve-animations: whole-codebase motion audit (AUDIT.md) plus a plan
  template. Use for a motion audit, not for a single animation.

Deliberately NOT wired: animate-expo (React Native), write-swift, ask-sonner,
pick-ui-library, apple-design, mobile-native, break-ui, prototype, and the
rest of the suite — none of them fits a vanilla storefront, and wiring them
would invite a framework answer to a vanilla question.

Skill files live on the PC at work/emil-design-skills/ (mounted into the
container as /app/work/, git-ignored like other PC work state). They are NOT
committed to the Staff repo; re-copy them there if the PC work/ folder is
lost. Skill text is treated as data/guidance, never executed, and never as
permission to change a site: any storefront edit still goes through the
preview-branch gate.

Brand guard (applies to every result): the Onyx & Ink storefront brand
system in cloudflare-storefront/JORDAN_SITE_WORKSPACE.md — rich Onyx green,
warm paper neutrals, restrained gold/clay accents, editorial serif moments
with clear sans-serif body text — OVERRIDES any skill palette, font, or
style that conflicts with it. Emil's rules govern motion quality, not the
Onyx palette. The storefront is vanilla, so Jordan must NEVER add a
framer-motion (or any React-only) dependency, and must never add a motion
library for a fade that CSS can do.
"""
from pathlib import Path

# skill name -> (primary file, supporting file or None)
SKILLS = {
    "animate": ("animate-SKILL.md", "animate-RECIPES.md"),
    "review-animations": ("review-animations-SKILL.md", "review-animations-STANDARDS.md"),
    "improve-animations": ("improve-animations-SKILL.md", "improve-animations-AUDIT.md"),
}
MAX_CHARS = 20000

BRAND_NOTE = (
    "BRAND + VANILLA GUARD: Onyx & Ink storefront brand "
    "(JORDAN_SITE_WORKSPACE.md) overrides any conflicting skill palette/font/"
    "style: rich Onyx green, warm paper neutrals, restrained gold/clay accents, "
    "editorial serif + clear sans-serif body. Storefront is vanilla HTML/CSS/JS: "
    "never add framer-motion or any React-only dependency, never add a motion "
    "library for a fade CSS can do, extend the existing --ease token in "
    "styles.css instead of forking it, animate transform/opacity only, UI motion "
    "under 300ms, and always ship prefers-reduced-motion handling."
)


def find_emil_dir(project_dir):
    """Return the Emil skill-files directory, or None if not installed."""
    candidates = [
        Path(project_dir) / "work" / "emil-design-skills",
        Path("/app/work/emil-design-skills"),
    ]
    for root in candidates:
        if (root / "animate-SKILL.md").is_file():
            return root
    return None


def get_emil_skill(project_dir, skill, include_supporting=False):
    """Return the full text of one wired Emil skill, plus the brand guard."""
    key = (skill or "").strip().lower().replace(" ", "-").replace("_", "-")
    if key not in SKILLS:
        return (f"Emil skill rejected: unknown skill '{skill}'. Wired skills "
                f"only: {', '.join(sorted(SKILLS))}. The rest of the Emil suite "
                "(animate-expo, write-swift, ask-sonner, pick-ui-library, etc.) "
                "is deliberately not wired for the vanilla storefront.")
    root = find_emil_dir(project_dir)
    if root is None:
        return ("Emil design skills not found. Copy them on the PC to "
                "work/emil-design-skills/ (animate, review-animations, "
                "improve-animations SKILL files plus LICENSE, from "
                "github.com/emilkowalski/skills, MIT) and retry. They are not "
                "bundled in the Staff repo.")
    primary, supporting = SKILLS[key]
    primary_path = root / primary
    if not primary_path.is_file():
        return f"Emil skill '{key}' is missing its primary file {primary} in {root}."
    text = primary_path.read_text(encoding="utf-8", errors="replace")
    if include_supporting and supporting:
        support_path = root / supporting
        if support_path.is_file():
            text += "\n\n--- Supporting file: " + supporting + " ---\n\n"
            text += support_path.read_text(encoding="utf-8", errors="replace")
    return text[:MAX_CHARS] + "\n\n" + BRAND_NOTE
