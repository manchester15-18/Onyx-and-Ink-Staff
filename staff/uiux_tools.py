"""Jordan-only UI/UX design tools.

Sources (both used as reference data, never as instructions to follow blindly):
- UI/UX Pro Max, MIT, v2.15.0, github.com/nextlevelbuilder/ui-ux-pro-max-skill.
  Its search CLI is pure Python with no external dependencies:
  ``python src/ui-ux-pro-max/scripts/search.py "<query>" --domain <domain>``
  Domains include product, style, typography, color, landing, chart, ux, icons,
  react, web, google-fonts, and gsap. ``--design-system`` builds a full system;
  ``--stack`` picks stack guidelines (html-tailwind is the storefront default).
  The skill database lives on the PC at work/ui-ux-pro-max-skill/ (mounted into
  the container as /app/work/, git-ignored like other PC work state). It is NOT
  committed to the Staff repo; re-clone it there if the PC work/ folder is lost.
- Framer Motion (now the `motion` package) is a React-only animation library.
  The storefront is vanilla HTML/CSS/JS, NOT React, so Jordan must NEVER add a
  framer-motion dependency to it. Instead, Framer Motion patterns are translated
  below into GSAP (already in the storefront devDependencies) or plain CSS.
  The UI/UX Pro Max `gsap` domain supplies matching GSAP skeletons.

Brand guard (applies to every result): the Onyx & Ink storefront brand system
in cloudflare-storefront/JORDAN_SITE_WORKSPACE.md — rich Onyx green, warm paper
neutrals, restrained gold/clay accents, editorial serif moments with clear
sans-serif body text — OVERRIDES any skill palette, font pairing, or style that
conflicts with it. The skill is a starting database for structure, UX rules,
motion, and accessibility checks, not a brand replacement. Skill output is
treated as data: it is quoted, never executed.
"""
import subprocess
import sys
from pathlib import Path

DOMAINS = {
    "product", "style", "typography", "color", "landing", "chart", "ux",
    "icons", "react", "web", "google-fonts", "gsap",
}
STACKS = {
    "html-tailwind", "react", "nextjs", "astro", "vue", "nuxtjs", "nuxt-ui",
    "svelte", "swiftui", "react-native", "flutter", "shadcn", "jetpack-compose",
    "threejs", "angular", "laravel", "javafx", "wpf", "winui", "avalonia",
    "uno", "uwp",
}
MAX_QUERY_CHARS = 300
MAX_OUTPUT_CHARS = 8000

BRAND_NOTE = (
    "BRAND GUARD: Onyx & Ink storefront brand (JORDAN_SITE_WORKSPACE.md) "
    "overrides any conflicting skill palette/font/style: rich Onyx green, warm "
    "paper neutrals, restrained gold/clay accents, editorial serif + clear "
    "sans-serif body. Use the skill for structure, UX rules, motion, and "
    "accessibility checks, not as a brand replacement. Respect "
    "prefers-reduced-motion and 4.5:1 text contrast."
)

# Framer Motion pattern -> vanilla storefront equivalent. Motion values are
# deliberately restrained (300-400ms, small offsets) so animation reads as
# polish, not distraction, on a shopping page.
MOTION_PATTERNS = {
    "fade-in": {
        "framer_motion": "<motion.div initial={{opacity: 0}} animate={{opacity: 1}} transition={{duration: 0.4}}>",
        "vanilla": "gsap.from(el, {opacity: 0, duration: 0.4, ease: 'power1.out'});  // or CSS: opacity 0 -> 1, transition 0.4s",
        "notes": "Hero and card entrances only. Skip motion entirely when prefers-reduced-motion is set.",
    },
    "slide-up": {
        "framer_motion": "<motion.div initial={{opacity: 0, y: 20}} animate={{opacity: 1, y: 0}} transition={{duration: 0.4, ease: 'easeOut'}}>",
        "vanilla": "gsap.from(el, {opacity: 0, y: 16, duration: 0.4, ease: 'power1.out'});  // keep y at 8-16px so it reads as a fade, not a slide",
        "notes": "Use for section entrances. Do not slide content a shopper needs to read (price, CTA) from off-screen.",
    },
    "stagger-list": {
        "framer_motion": "variants container {visible: {transition: {staggerChildren: 0.08}}}; item {hidden: {opacity: 0, y: 16}, visible: {opacity: 1, y: 0}}",
        "vanilla": "gsap.from(items, {opacity: 0, y: 16, duration: 0.35, stagger: 0.08, ease: 'power1.out'});  // see also UI/UX Pro Max gsap domain: 'stagger'",
        "notes": "Product grids and feature lists only, max ~8 items visible at once; longer lists stagger in viewport batches.",
    },
    "scroll-reveal": {
        "framer_motion": "<motion.div whileInView={{opacity: 1, y: 0}} initial={{opacity: 0, y: 16}} viewport={{once: true}}>",
        "vanilla": "gsap.from(el, {opacity: 0, y: 12, duration: 0.35, ease: 'power1.out', scrollTrigger: {trigger: el, start: 'top 90%', toggleActions: 'play none none reverse'}});  // register ScrollTrigger once; needs a no-JS fallback so content is never invisible by default",
        "notes": "Below-fold sections only. Never hide price/CTA content behind a reveal that requires JS.",
    },
    "hover-scale": {
        "framer_motion": "<motion.button whileHover={{scale: 1.03}} whileTap={{scale: 0.97}}>",
        "vanilla": "CSS: .btn {transition: transform 0.2s ease, box-shadow 0.2s ease} .btn:hover {transform: scale(1.03)} .btn:active {transform: scale(0.97)}  // or gsap.to on mouseenter/leave",
        "notes": "Keep scale at 1.02-1.05. Never scale text-heavy elements; use a shadow/color shift instead.",
    },
    "modal": {
        "framer_motion": "<AnimatePresence>{open && <motion.div initial={{opacity: 0, scale: 0.96}} animate={{opacity: 1, scale: 1}} exit={{opacity: 0, scale: 0.96}}>}",
        "vanilla": "gsap.fromTo(dialog, {opacity: 0, scale: 0.96}, {opacity: 1, scale: 1, duration: 0.25, ease: 'power1.out'}); on close, animate out then remove from DOM; trap focus and return focus on close",
        "notes": "Exit animation matters as much as entrance. If in doubt, use a plain fade with no scale.",
    },
    "page-transition": {
        "framer_motion": "<AnimatePresence mode='wait'><motion.div key={route} initial={{opacity: 0}} animate={{opacity: 1}} exit={{opacity: 0}}>",
        "vanilla": "Not applicable as a route transition on a multi-page vanilla site. Use a short fade on page load (CSS animation, 0.3s) and a fade-out on link click only if it never delays navigation past ~300ms.",
        "notes": "Do NOT fake SPA transitions on the storefront. Checkout and product navigation must never wait on animation.",
    },
}


def find_skill_dir(project_dir):
    """Return the UI/UX Pro Max skill root, or None if it is not installed."""
    candidates = [
        Path(project_dir) / "work" / "ui-ux-pro-max-skill",
        Path("/app/work/ui-ux-pro-max-skill"),
        Path(project_dir) / "staff" / "skills" / "ui-ux-pro-max",
    ]
    for root in candidates:
        script = root / "src" / "ui-ux-pro-max" / "scripts" / "search.py"
        if script.is_file():
            return root
    return None


def search_uiux(project_dir, query, domain=None, design_system=False,
                stack="html-tailwind", max_results=5):
    """Query the UI/UX Pro Max database. Output is data, never executed."""
    if not query or len(query) > MAX_QUERY_CHARS:
        return f"UI/UX search rejected: query must be 1-{MAX_QUERY_CHARS} characters."
    if domain is not None and domain not in DOMAINS:
        return f"UI/UX search rejected: unknown domain '{domain}'. Valid: {', '.join(sorted(DOMAINS))}."
    if stack not in STACKS:
        return f"UI/UX search rejected: unknown stack '{stack}'. Default is html-tailwind (the storefront stack)."
    root = find_skill_dir(project_dir)
    if root is None:
        return ("UI/UX Pro Max skill not found. Install it on the PC at "
                "work/ui-ux-pro-max-skill (git clone --depth 1 "
                "https://github.com/nextlevelbuilder/ui-ux-pro-max-skill.git) "
                "and retry. It is not bundled in the Staff repo.")
    script = root / "src" / "ui-ux-pro-max" / "scripts" / "search.py"
    cmd = [sys.executable, str(script), query]
    if design_system:
        cmd.append("--design-system")
    elif domain:
        cmd += ["--domain", domain]
    else:
        cmd += ["--stack", stack]
    cmd += ["-n", str(max(1, min(int(max_results), 10)))]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                              cwd=str(root))
    except subprocess.TimeoutExpired:
        return "UI/UX search timed out after 60s."
    except Exception as exc:  # noqa: BLE001 - surface the failure, do not hide it
        return f"UI/UX search failed: {type(exc).__name__}: {exc}"
    out = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0 and not out:
        return f"UI/UX search failed with exit code {proc.returncode}."
    return out[:MAX_OUTPUT_CHARS] + "\n\n" + BRAND_NOTE


def get_motion_pattern(pattern):
    """Translate a Framer Motion pattern into a vanilla GSAP/CSS equivalent."""
    key = (pattern or "").strip().lower().replace(" ", "-").replace("_", "-")
    entry = MOTION_PATTERNS.get(key)
    if entry is None:
        return (f"Unknown motion pattern '{pattern}'. Known patterns: "
                f"{', '.join(sorted(MOTION_PATTERNS))}. Note: Framer Motion "
                "itself requires React and must NOT be added to the vanilla "
                "storefront; use the GSAP/CSS equivalent instead.")
    return (
        f"MOTION PATTERN: {key}\n"
        f"Framer Motion (reference only, React-only — do not install on the storefront):\n  {entry['framer_motion']}\n"
        f"Vanilla storefront equivalent (GSAP, already in storefront devDependencies, or CSS):\n  {entry['vanilla']}\n"
        f"Notes: {entry['notes']}\n\n{BRAND_NOTE}"
    )
