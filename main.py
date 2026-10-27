"""Run the Onyx and Ink staff on Groq. Configuration lives in .env."""
import argparse
import fcntl
import json
import os
import re
import threading
from types import SimpleNamespace
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

# Keep CrewAI runtime data inside this project, before importing CrewAI.
PROJECT_DIR = Path(__file__).resolve().parent
BUSINESS_TZ = ZoneInfo('America/New_York')
os.environ.setdefault("CREWAI_STORAGE_DIR", str(PROJECT_DIR / "work" / "crewai-storage"))
os.environ["CREWAI_TELEMETRY_DISABLED"] = "true"
os.environ["CREWAI_TRACING_ENABLED"] = "false"
os.environ.setdefault("OTEL_SDK_DISABLED", "true")


from dotenv import load_dotenv, dotenv_values
from crewai import Agent, Task
from crewai.tools import tool
from openai import APIConnectionError, APIStatusError
from staff.groq_llm import GroqLLM
from staff.model_routing import RoutingError
from staff.staff_email import StaffMail
from staff.report_format import normalize_report, standardize_staff_report
from staff.report_projects import active_project, ensure_project, consolidate_assignment_reports, project_context
from staff.staff_tasks import claim as claim_staff_task, complete as complete_staff_task, release as release_staff_task
from staff.web_research import WebResearchError, search as tavily_search, verify as verify_tavily

DEFAULT_DIRECTIVE = "Plan our upcoming custom gift product push for Onyx and Ink. Provide a coordinated operational plan."
DEPARTMENT_RULES = (
    "Department ownership is immutable: Avery owns Marketing only; Jordan owns Web Development and IT only; "
    "Cameron owns HR and Legal only; Morgan owns COO coordination only. Morgan may split cross-functional work "
    "into department-aligned pieces but may never swap, rename, or reassign these departments."
)
SAMPLE_INVENTORY = {
    "tumbler": "240 units (20oz Stainless Steel - White)",
    "keychain": "500 units (Acrylic Clear Blanks)",
    "shirt": "150 units (Heavy Cotton Crewneck - Black)",
    "puzzle": "85 units (120-piece Sublimation Blanks)",
    "bookmark": "300 units (Aluminum Gloss)",
}


def _duration_seconds(value):
    match=re.search(r'(?:(\d+)h)?(?:(\d+)m)?([\d.]+)s',str(value or ''),re.I)
    if not match:return None
    return int(match.group(1) or 0)*3600+int(match.group(2) or 0)*60+float(match.group(3))


def quota_retry_seconds(error):
    """Read Groq recovery guidance from headers or its bounded error message."""
    candidates=[];description=str(error).lower()
    response=getattr(error,'response',None)
    headers=getattr(response,'headers',{}) or {}
    request_limited=any(phrase in description for phrase in ('request limit','requests per','request capacity')) and 'token' not in description
    reset_key='x-ratelimit-reset-requests' if request_limited else 'x-ratelimit-reset-tokens'
    if not headers.get(reset_key):
        reset_key='x-ratelimit-reset-requests' if reset_key.endswith('tokens') else 'x-ratelimit-reset-tokens'
    for key in ('retry-after',reset_key):
        value=headers.get(key)
        if value:
            try:candidates.append(float(value))
            except (TypeError,ValueError):
                parsed=_duration_seconds(value)
                if parsed is not None:candidates.append(parsed)
    for value in re.findall(r'try again in\s+([\dhms.]+)',str(error),re.I):
        parsed=_duration_seconds(value)
        if parsed is not None:candidates.append(parsed)
    # A missing reset hint should pause rather than hammer a daily allowance.
    return max(60,min(86400,int(max(candidates,default=1800)+30)))


def save_provider_cooldown(root,seconds):
    path=Path(root)/'work'/'groq-cooldown.json';path.parent.mkdir(exist_ok=True)
    ready=time.time()+seconds
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps({'readyEpoch':ready,'reason':'usage-limit'},indent=2));temporary.chmod(0o600);temporary.replace(path)
    return ready


@tool("Search the live web with Tavily")
def search_web(query: str) -> str:
    """Search current public web sources using one JSON object: {"query": "specific search"}.
    Results include titles, URLs, and concise source text. Cite returned URLs.
    """
    try:
        return tavily_search(PROJECT_DIR, query, max_results=3)
    except (ValueError, WebResearchError) as error:
        return f"WEB SEARCH UNAVAILABLE: {error} Continue with labeled assumptions and do not retry this search."


@tool("Check sample blank inventory")
def check_blank_stock(item_names: list[str]) -> str:
    """Look up SAMPLE inventory using one JSON object: {"item_names": ["shirt", "tumbler"]}.
    These counts are demonstration data, not a connection to the warehouse.
    """
    aliases = {"tumblers": "tumbler", "keychains": "keychain", "shirts": "shirt", "t-shirt": "shirt", "t_shirts": "shirt", "puzzles": "puzzle", "bookmarks": "bookmark"}
    lines = ["SAMPLE INVENTORY — confirm actual counts before making commitments."]
    for name in item_names:
        normalized = name.strip().lower().replace('_',' ').replace('-',' ')
        normalized = aliases.get(normalized, normalized)
        normalized = next((item for item in SAMPLE_INVENTORY if item in normalized),normalized)
        if normalized == "all":
            lines.extend(f"{item}: {stock}" for item, stock in SAMPLE_INVENTORY.items())
        else:
            lines.append(f"{normalized}: {SAMPLE_INVENTORY.get(normalized, 'Not found in sample inventory.')}")
    return "\n".join(lines)


def credential(name, required=True):
    value = os.getenv(name, "")
    if not value:
        if required:
            raise ValueError(f"Set {name} in the project's .env file. See README.md.")
        return None
    if not value.isascii() or any(c.isspace() for c in value) or any(c in value for c in '\"\''):
        raise ValueError(f"{name} contains quotes, whitespace, or non-ASCII characters. Paste the key without quotes.")
    if "paste" in value.lower() or "your_key" in value.lower():
        raise ValueError(f"Replace the placeholder for {name} with your API key.")
    return value


def positive_int(name, default):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be a positive whole number.") from None
    if value <= 0:
        raise ValueError(f"{name} must be a positive whole number.")
    return value


def verified_search_key(search_key):
    """Reject unusable Tavily credentials before agents enter a tool loop."""
    if not search_key:
        return None
    try:
        verify_tavily(search_key)
    except WebResearchError as error:
        print(f"Web search disabled for this run: {error} Update TAVILY_API_KEY in dashboard Settings; the agents will continue without research.")
        return None
    return search_key


def enforce_department_ownership(value):
    """Correct owner labels if a model pairs a department with the wrong agent."""
    text = str(value)
    departments = (
        (r"Marketing", "Marketing", "Avery"),
        (r"Web\s*(?:Development|Dev)|IT|Engineering", "Web Development / IT", "Jordan"),
        (r"HR\s*(?:/|&|and)\s*Legal|Legal\s*(?:/|&|and)\s*HR|Legal|Human Resources", "HR / Legal", "Cameron"),
    )
    for pattern, label, owner in departments:
        text = re.sub(
            rf"(?:{pattern})\s*\(\s*(?:Avery|Jordan|Cameron)\s*\)",
            f"{label} ({owner})",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            rf"(?:Avery|Jordan|Cameron)\s*\(\s*(?:{pattern})\s*\)",
            f"{owner} ({label})",
            text,
            flags=re.IGNORECASE,
        )
    return text


def assignment_output_valid(value):
    raw=str(getattr(value,'raw',value) or '')
    final=re.search(r'(?is)\bFinal Answer\s*:\s*(.+)$',raw)
    if final:raw=final.group(1)
    return not bool(re.match(r'(?is)^\s*(?:Thought|Action)\s*:',raw)) and bool(raw.strip())


def finalize_assignment_report(path,agent,value=None):
    """Publish only a finished report; discard leaked model drafting text."""
    path=Path(path);raw=str(getattr(value,'raw',value) or '') if value is not None else (path.read_text() if path.exists() else '')
    final=re.search(r'(?is)\bFinal Answer\s*:\s*(.+)$',raw)
    if final:raw=final.group(1)
    invalid=bool(re.match(r'(?is)^\s*(?:Thought|Action)\s*:',raw))
    if invalid or not raw.strip():
        cleaned=(f'# {agent} Assignment Needs Retry\n\n**Status**\n'
                 '- The unfinished model drafting text was discarded and was not published as a staff report.\n\n'
                 '**Next Action**\n- This assignment will be retried automatically during authorized working hours.')
    else:
        cleaned=standardize_staff_report(agent+' Department Update',enforce_department_ownership(raw),agent)
        cleaned=re.sub(r'(?i)\bcampaign cycle\b','campaign',cleaned)
        cleaned=re.sub(r'(?i)\bwork cycle\b','assignment',cleaned)
    path.write_text(cleaned.rstrip()+'\n');path.chmod(0o600)
    return not invalid and bool(raw.strip())


class WorkProgress:
    """Private per-agent work status used by the dashboard."""
    def __init__(self,root,assignment_id):
        self.path=Path(root)/'work'/'staff-work-status.json';self.assignment_id=assignment_id;self.lock=threading.Lock()
        self.agents={name:'free' for name in ('Morgan','Avery','Jordan','Cameron')}
        self.agent_details={name:'Waiting for the next assignment.' for name in self.agents}
    def write(self,status='working',detail='Staff are completing independent assignments.'):
        with self.lock:
            data={'assignment':self.assignment_id,'status':status,'detail':detail,'agents':dict(self.agents),'agentDetails':dict(self.agent_details),'updated':datetime.now(BUSINESS_TZ).isoformat(timespec='seconds')}
            self.path.parent.mkdir(exist_ok=True);temp=self.path.with_suffix('.tmp');temp.write_text(json.dumps(data,indent=2));temp.chmod(0o600);temp.replace(self.path)
    def start(self,name,detail):
        with self.lock:
            self.agents[name]='working';self.agent_details[name]=detail
        self.write(detail=name+' is working. Other staff remain available until their assignment starts.')
    def callback(self,name,after=None):
        def done(output):
            if after:after(output)
            with self.lock:
                self.agents[name]='free';self.agent_details[name]='Finished the latest assignment and is ready for the next one.'
            self.write(detail=name+' completed an assignment; other staff continue independently.')
        return done


class DirectAutonomousAssignment:
    """One bounded cloud request for free-tier autonomous report production."""
    def __init__(self, llm, agent, directive, focus, output_file, project_brief='', assigned_task=None):
        self.llm=llm; self.agent=SimpleNamespace(llm=llm); self.name=agent; self.output_file=str(output_file)
        self.assigned_task=assigned_task
        task_context=''
        if assigned_task:
            task_context=(f"\n\nA current assignment from {assigned_task['from']} is assigned to you: "
                                                    f"{assigned_task['text']}\nAddress this assignment directly in the deliverable. ")
            if re.search(r'privacy\s+policy|privacy\s+notice', str(assigned_task.get('text','')), re.I):
                task_context += ("This is a full policy draft, not a status update. Include complete sections for scope, data collected, "
                                 "uses, sharing and processors, retention and security, customer rights and choices, cookies, and contact/updates. "
                                 "End with a clear professional-review notice. Keep the entire draft below 700 words; use short bullets instead of tables so every required section is completed. Do not stop after an outline or an unfinished bullet. ")
        self.prompt=(f"You are {agent} at Onyx & Ink. {focus}\nCEO objective: {directive[:650]}\n\nCurrent approved project briefing:\n{project_brief[:3500]}\n"
                     +task_context+
                     "Produce one usable departmental deliverable now, with the actual draft, checklist, copy, decision register, or acceptance criteria in the report. "
                     "Use only the stated project context. Never claim that you reviewed metrics, contacted anyone, sent a message, changed a system, or completed outside work unless that evidence is supplied. "
                     "When facts are missing, label assumptions and create reusable draft material instead of inventing business data, people, dates, policies, or prior work. "
                     "Return plain text only. Use short title-style lines and simple hyphen bullets when useful; do not use Markdown markers, tables, code fences, or decorative separators. Include Deliverable, Assumptions, Next action, and Open decision. Do not use tools, explain reasoning, or promise unperformed work.")
    def execute_sync(self):
        answer=self.llm.call([{'role':'user','content':self.prompt}])
        Path(self.output_file).write_text(answer.strip()+'\n')
        return answer


@tool("Generate a text-free tumbler design")
def generate_tumbler_design(prompt: str) -> str:
    """Generate a TEXT-FREE tumbler design with Cloudflare Flux. Input is a plain prompt string.
    The tool appends no-text + tumbler-only guardrails automatically. Never ask it to render text, a brand name, or a personalization — add text afterwards with the overlay tool.
    Returns the saved PNG path in work/artifacts/.
    """
    from staff.design_images import generate_text_free
    import hashlib, time
    data = generate_text_free(PROJECT_DIR, prompt)
    name = hashlib.sha256(data).hexdigest()[:16] + ".png"
    out = PROJECT_DIR / "work" / "artifacts" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return f"TEXT-FREE DESIGN SAVED: {out} — add any text with the overlay tool, then run the pre-publish checklist before publishing."

@tool("Overlay real text on a design")
def overlay_design_text(image_path: str, text: str) -> str:
    """Overlay REAL text on a design image with a real font (Pillow). This is the ONLY way to add a name, brand, or phrase to a design — never let an image model render text.
    Input: image_path (existing PNG/JPEG in work/artifacts/) and text (max 60 characters).
    Returns the saved PNG path.
    """
    from staff.design_tools import overlay_text
    from pathlib import Path as _P
    src = _P(image_path)
    out = src.parent / (src.stem + "-overlay.png")
    return f"OVERLAY SAVED: {overlay_text(src, out, text)}"

@tool("Build a 300-DPI print wrap")
def build_print_wrap(image_path: str) -> str:
    """Build a 300-DPI print wrap (2790x2460 px) from a design image. The template is a common 20oz straight skinny starting size (9.3 x 8.2 in), NOT a verified blank size — measure the actual blank before printing.
    Returns the saved PNG path and dimensions.
    """
    from staff.design_tools import build_wrap
    from pathlib import Path as _P
    src = _P(image_path)
    out = src.parent / (src.stem + "-wrap.png")
    result = build_wrap(src, out)
    return f"WRAP SAVED: {result['output']} ({result['width_px']}x{result['height_px']} px at {result['dpi']} DPI). WARNING: {result['warning']}"

@tool("Run the pre-publish design checklist")
def design_checklist(image_path: str) -> str:
    """Run the pre-publish checklist on a design image. A design passes ONLY if it is a tumbler (human-confirmed), has no AI-rendered text, and is PNG/JPEG at mockup resolution or better.
    Input: image_path. This tool cannot auto-confirm the product — Avery must look at the image and confirm it is a tumbler before claiming a pass.
    """
    from staff.design_tools import pre_publish_checklist
    passed, checks = pre_publish_checklist(image_path)
    lines = [f"PRE-PUBLISH CHECKLIST: {'PASS' if passed else 'FAIL'}"]
    lines.extend(f"{status}: {name} — {detail}" for status, name, detail in checks)
    return "\n".join(lines)

@tool("Critique a design")
def critique_design_tool(image_path: str, product: str, verdict: str, what_was_right: str, what_was_wrong: str, suggested_fix: str, agent: str = "Avery") -> str:
    """Critique a generated design and turn the critique into a lesson for the next generation. Avery must look at the image first and report honestly what is right and what is wrong.
    Inputs: image_path, product name, verdict (pass/partial/fail), what_was_right, what_was_wrong, suggested_fix (the prompt change that should prevent the wrong part next time; empty if none).
    The critique is saved to work/design-critiques.jsonl and the suggested fix becomes a lesson in work/design-lessons.json, automatically applied to the next generation for that product.
    """
    from staff.design_learning import critique_design
    record = critique_design(PROJECT_DIR, image_path, product, verdict, what_was_right, what_was_wrong, suggested_fix=suggested_fix, text_found=("text" in what_was_wrong.lower() or "$" in what_was_wrong), agent=agent)
    return f"CRITIQUE SAVED: {record['verdict']} for {product}. Lessons added: {record['lessons_added'] or 'none'}. Checks: {record['checks']}"

@tool("Generate a design with learned lessons")
def generate_learned_design(prompt: str, product: str, agent: str = "Avery") -> str:
    """Generate a design with all lessons learned from past critiques automatically applied. Use this for every new design, not the plain generation tool, so Avery keeps improving.
    Inputs: prompt (plain description) and product name (e.g. tumbler, snow globe tumbler, keychain, pen, luggage tag, shirt, puzzle, bookmark, ornament).
    Returns the saved PNG path in work/artifacts/.
    """
    from staff.design_images import generate_text_free
    import hashlib
    data = generate_text_free(PROJECT_DIR, prompt, product=product, agent=agent)
    name = hashlib.sha256(data).hexdigest()[:16] + ".png"
    out = PROJECT_DIR / "work" / "artifacts" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return f"LEARNED DESIGN SAVED: {out} — critique it with the critique tool before publishing, so the next design gets even better."

@tool("Visually inspect a design or file")
def inspect_design_tool(image_path: str, expected_product: str, agent: str) -> str:
    """Visually inspect an image file with a vision model. Use this for any design you generated, any file James or Donald placed in work/, or any downloaded image — do not claim a product, text, or brand fit you have not visually checked.
    Inputs: image_path (PNG/JPEG/WebP in work/, e.g. work/artifacts/...), expected_product (what the image was supposed to be, or empty), agent (your own name: Avery, Jordan, Cameron, or Morgan).
    Returns the product seen, ALL text exactly as spelled, blank personalization area, brand fit, verdict, and a suggested prompt fix. Feed that result into the critique tool so the next generation improves.
    """
    from staff.vision_inspect import inspect_image
    result = inspect_image(PROJECT_DIR, image_path, agent=agent or "Avery", expected=expected_product or None)
    return f"VISION INSPECTION by {result['agent']} for {result['image']}: product seen: {result['product_seen']}; text seen: {result['text_seen']}; blank area: {result['blank_area']}; brand fit: {result['brand_fit']}; verdict: {result['verdict_line']}; suggested fix: {result['suggested_fix']}. Raw: {result['raw']}"

@tool("Approve a design as a quality reference")
def approve_design_tool(image_path: str, product: str, notation: str, agent: str = "Avery", as_reference: str = "no") -> str:
    """Approve a verified, high-quality design and optionally mark it as a quality reference standard for its product. Only approve after vision inspection + checklist + critique all pass, or after a human has looked at it — never approve your own unchecked design, and never approve a design with AI text or the wrong product.
    Inputs: image_path, product, notation (why this is a good standard, what future designs should match), agent (your name), as_reference ("yes" to mark it as the standard, "no" for approved-only).
    The approver is recorded as the calling agent unless a human approver is named in the notes — James's sign-off is the true production standard and he can revoke any approval.
    """
    from staff.design_approval import approve_design
    record = approve_design(PROJECT_DIR, image_path, product, approver=agent, agent=agent,
                            verified_by=["vision", "checklist", "critique"], notation=notation,
                            notes=f"Approved via CrewAI tool by {agent}.", as_reference=(as_reference.lower() == "yes"))
    return f"APPROVAL SAVED: {record['image_path']} status={record['status']} reference={record['reference']} notation={record['notation']}"

@tool("List approved reference designs")
def list_references_tool(product: str) -> str:
    """List approved reference designs, optionally for one product. Use this before generating or judging a design, so you know the standard to match.
    Input: product name, or empty for all references.
    """
    from staff.design_approval import get_references
    refs = get_references(PROJECT_DIR, product or None)
    if not refs:
        return "No approved references found."
    lines = [f"REFERENCES ({len(refs)}):"]
    for r in refs:
        lines.append(f"- {r['product']}: {r['image_path']} — {r['notation']} (approver: {r['approver']})")
    return "\n".join(lines)

@tool("Compare a design to its reference standard")
def compare_reference_tool(image_path: str, product: str, agent: str = "Avery") -> str:
    """Vision-compare a candidate design against the approved reference standard for its product. Use the first reference for that product; if none exists, say so and do not invent a standard.
    Inputs: image_path (candidate), product, agent (your name).
    Returns whether the candidate meets the reference standard, with differences.
    """
    from staff.design_approval import get_references
    from staff.vision_inspect import compare_to_reference
    refs = get_references(PROJECT_DIR, product)
    if not refs:
        return f"No approved reference exists for {product} — cannot compare. Approve a reference first."
    result = compare_to_reference(PROJECT_DIR, image_path, refs[0]["image_path"], agent=agent, expected=product)
    return f"REFERENCE COMPARISON vs {refs[0]['image_path']}: verdict={result['verdict']}. Raw: {result['raw']}"

@tool("Screenshot a website")
def screenshot_website_tool(url: str, agent: str = "Jordan") -> str:
    """Screenshot a website's customer view in a real browser and save the PNG. Use this before claiming anything about how a page looks — raw HTML is not a customer view.
    Input: url (full http(s) URL, e.g. the storefront preview). Returns the saved PNG path in work/website-audits/, page title, and final URL after redirects. Then use the website audit tool to inspect that screenshot with vision.
    """
    from staff.website_inspect import screenshot_website
    from datetime import datetime, timezone
    from urllib.parse import urlparse
    import re
    host = re.sub(r"[^A-Za-z0-9-]+", "-", urlparse(url).netloc.replace(".", "-")).strip("-") or "website"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = PROJECT_DIR / "work" / "website-audits" / f"{host}-{stamp}.png"
    shot = screenshot_website(url, out)
    return f"WEBSITE SCREENSHOT: {shot['path']} — title: {shot['title'] or 'unknown'}, final URL: {shot['final_url']}. Audit it with the website audit tool before reporting on the page."

@tool("Visually audit a website (customer view)")
def audit_website_tool(url: str, agent: str = "Jordan") -> str:
    """Visually audit a website as a customer sees it: screenshot it in a real browser, then inspect the screenshot with a vision model for navigation, products/prices/CTAs, broken images, overlapping text, brand fit, and shopper-trust issues.
    Input: url (full http(s) URL). Returns verdict PASS/PARTIAL/FAIL, issues, prioritised fixes, and the screenshot path as evidence. Never claim a page looks right without this audit.
    """
    from staff.website_inspect import inspect_website
    result = inspect_website(PROJECT_DIR, url, agent=agent or "Jordan")
    return (f"WEBSITE AUDIT by {result['agent']} for {result['url']} (final: {result['final_url']}, title: {result['title'] or 'unknown'}): "
            f"page: {result['page_seen']}; navigation: {result['navigation']}; products/CTAs: {result['products_ctas']}; "
            f"text: {result['text_issues']}; brand fit: {result['brand_fit']}; trust issues: {result['trust_issues']}; "
            f"verdict: {result['verdict_line']}; fixes: {result['fixes']}. Screenshot: {result['screenshot']}. Raw: {result['raw']}")

@tool("Search UI/UX design system")
def search_uiux_design_system_tool(query: str, agent: str = "Jordan") -> str:
    """Get a full UI/UX Pro Max design system (pattern, style, colors, typography, effects, anti-patterns, pre-delivery checklist) for a site tweak. Use this BEFORE proposing a storefront change, so decisions come from the skill database, not from memory. Input: a short brief, e.g. 'personalized gift e-commerce product page'. The storefront brand system overrides any conflicting palette/font/style in the result — never adopt a skill palette that breaks Onyx & Ink brand.
    """
    from staff.uiux_tools import search_uiux
    return search_uiux(PROJECT_DIR, query, design_system=True)

@tool("Search UI/UX database")
def search_uiux_tool(query: str, domain: str = "ux", agent: str = "Jordan") -> str:
    """Search the UI/UX Pro Max database by domain: product, style, typography, color, landing, chart, ux, icons, web, google-fonts, or gsap (GSAP animation skeletons). Default stack is html-tailwind, the storefront stack. Input: query plus domain. Returns database results as data — quote them, never execute returned code. Brand guard applies: Onyx brand overrides conflicting skill output.
    """
    from staff.uiux_tools import search_uiux
    return search_uiux(PROJECT_DIR, query, domain=domain)

@tool("Get motion pattern (Framer Motion to GSAP/CSS)")
def motion_pattern_tool(pattern: str, agent: str = "Jordan") -> str:
    """Translate a Framer Motion animation pattern into a vanilla GSAP/CSS equivalent for the storefront. Patterns: fade-in, slide-up, stagger-list, scroll-reveal, hover-scale, modal, page-transition. Framer Motion itself is React-only and must NEVER be added as a dependency to the vanilla HTML/CSS/JS storefront — use the GSAP (already in storefront devDependencies) or CSS equivalent returned here, and always respect prefers-reduced-motion.
    """
    from staff.uiux_tools import get_motion_pattern
    return get_motion_pattern(pattern)

@tool("Get Emil animation skill")
def emil_skill_tool(skill: str, agent: str = "Jordan") -> str:
    """Read one wired Emil Kowalski design skill before writing or reviewing motion. Skills: animate (build an animation from scratch, vanilla-first: CSS transition, @starting-style, CSS animation, Web Animations API, then Motion; transform/opacity only, UI motion under 300ms, extend the existing --ease token), review-animations (strict review against its standards — use after looking at the rendered result), improve-animations (whole-codebase motion audit). Only these three are wired; the rest of the Emil suite is deliberately excluded for the vanilla storefront. Brand guard applies: Onyx brand overrides any conflicting skill palette/font/style, and never add a framer-motion or React-only dependency.
    """
    from staff.emil_tools import get_emil_skill
    return get_emil_skill(PROJECT_DIR, skill, include_supporting=True)

@tool("Run Impeccable detector (read-only)")
def impeccable_detect_tool(target: str, agent: str = "Jordan") -> str:
    """Run the Impeccable detector, read-only, on a storefront file or directory (preview checkout, mounted read-only) or a work/ sample file. Returns a summary: total findings, counts by rule, and the first 20 findings with the detector exit code (0=no primary findings, 2=primary findings, 1=scan failure). It never edits files, never uses live mode, and never injects anything into a deployed site. Findings are reported, not auto-ignored; brand-deliberate waivers belong in the storefront .impeccable/config.json with a stated reason. Input: a relative target such as 'index.html' or 'products/'.
    """
    from staff.impeccable_tools import detect_impeccable
    return detect_impeccable(PROJECT_DIR, target)

def build_assignments(llm, directive, report_dir, search_key=None, verbose=False, mail=None, progress=None, project_brief='', root=None, project_id=''):
    # CrewAI adds a large control prompt and multi-turn loop which the free cloud
    # routes reject before publishing a report. Autonomous work deliberately uses
    # a single bounded request; interactive staff retain their tool-capable flow.
    if isinstance(llm, __import__('staff.model_routing', fromlist=['RoutedLLM']).RoutedLLM):
        focuses={
            'Morgan':'Create a current dependency and decision register for the active project, with owners and next steps only.',
            'Avery':'Draft usable marketing copy or a campaign asset for the active project; do not claim campaign performance.',
            'Jordan':'Create implementation-ready technical requirements, acceptance criteria, or a deployment checklist for the active project.',
            'Cameron':'Draft cautious customer-policy, HR, or compliance language relevant to the active project; flag it for professional review where appropriate.'}
        files={'Morgan':'morgan-operations.md','Avery':'avery-marketing.md','Jordan':'jordan-web-it.md','Cameron':'cameron-hr-legal.md'}
        tasks={}
        for name in focuses:
            assigned_task=claim_staff_task(root,name,project_id) if root else None
            agent_llm=llm.for_agent(name)
            task_text=str((assigned_task or {}).get('text',''))
            # Policies need enough room for complete, reviewable sections. Short
            # report limits are suitable for routine updates but can silently cut
            # a legal draft off mid-section.
            if name == 'Cameron' and re.search(r'privacy\s+policy|privacy\s+notice', task_text, re.I):
                agent_llm.max_tokens=max(agent_llm.max_tokens, 1600)
            tasks[name]=DirectAutonomousAssignment(agent_llm,name,directive,focuses[name],report_dir/files[name],project_brief,assigned_task)
        if progress:
            for name,assignment in tasks.items():
                original=assignment.execute_sync
                def execute(run=original, callback=progress.callback(name), agent_name=name):
                    current=tasks[agent_name].assigned_task
                    detail=(current['from']+' assigned: '+current['text'][:220]) if current else ('Creating the current '+agent_name+' project deliverable.')
                    progress.start(agent_name,detail)
                    result=run();callback(result);return result
                assignment.execute_sync=execute
        return tasks
    search_tools = [search_web] if search_key else []
    rules = (
        DEPARTMENT_RULES+" "
        "Use one JSON object matching the schema per tool call; never a top-level array. "
        "Keep tool calls and reports concise. Distinguish facts, sample inventory, assumptions, and recommendations. "
        "Never invent numeric targets, budgets, deadlines, capacity, conversion rates, revenue, inventory, or performance results. Use a number only when it comes from the CEO directive, the sample inventory tool, or a cited current source. Otherwise say the value is unknown or label it 'Proposed target — CEO approval required.' "
        "Cite URLs for researched claims. Never invent search results or claim changes were deployed. "
        "Start the work immediately. Choose and complete a concrete useful next step with the tools available; do not wait for another agent or an email unless a decision truly blocks progress. "
        "Keep any required Thought under 25 words, then immediately use a tool or provide the Final Answer. Never return reasoning-only output. Never refer to work as a cycle; call it an assignment. "
        "If web search reports unavailable, do not retry it; continue with clearly labeled assumptions."
    )
    # Autonomous passes are a concise report/update, not an open-ended chat.
    # One deliberate model response prevents Crew from spending the whole free
    # tier on self-directed follow-up turns before it publishes any work.
    common = dict(llm=llm, allow_delegation=False, max_retry_limit=0, max_iter=1, verbose=verbose)

    marketing = Agent(role="Avery - Marketing - Onyx and Ink", goal="Complete marketing, brand, campaign, audience, merchandising, and customer messaging work.", backstory="Your name is Avery. You own Marketing only. You do not accept Legal, HR, Web Development, or IT ownership. You oversee customizable tumblers, shirts, keychains, puzzles, and bookmarks from the marketing perspective. " + rules, tools=[check_blank_stock, *search_tools, generate_tumbler_design, overlay_design_text, build_print_wrap, design_checklist, critique_design_tool, generate_learned_design, inspect_design_tool, approve_design_tool, list_references_tool, compare_reference_tool], **common)
    web = Agent(tools=[inspect_design_tool, critique_design_tool, generate_learned_design, approve_design_tool, list_references_tool, compare_reference_tool, screenshot_website_tool, audit_website_tool, search_uiux_design_system_tool, search_uiux_tool, motion_pattern_tool, emil_skill_tool, impeccable_detect_tool], role="Jordan - Web Development and IT - Onyx and Ink", goal="Complete website, storefront, dashboard, integration, infrastructure, security, and technical specification work.", backstory="Your name is Jordan. You own Web Development and IT only. You do not accept Marketing, HR, or Legal ownership. You prepare implementation requirements for the storefront and internal systems. You can visually inspect a website's customer view with the website screenshot and audit tools — screenshot first, then audit, and never claim a page looks right without seeing it. For site tweaks, use UI/UX Pro Max first (design-system, database, and GSAP searches) so decisions come from the skill database, not from memory, and translate any Framer Motion idea into GSAP/CSS — the storefront is vanilla HTML/CSS/JS, not React, so never add a framer-motion dependency. For motion, read the wired Emil Kowalski skills first (animate to build, review-animations to judge, improve-animations to audit a whole codebase): vanilla-first, transform/opacity only, UI motion under 300ms, extend the existing --ease token, always ship prefers-reduced-motion handling. For quality gates, run the Impeccable detector (read-only) on the preview checkout and report its findings honestly — never auto-ignore a real finding, never use live mode. The Onyx & Ink brand system in JORDAN_SITE_WORKSPACE.md overrides any skill palette, font, or style that conflicts with it. " + rules, **common)
    legal = Agent(role="Cameron - HR and Legal - Onyx and Ink", goal="Complete HR, policy, compliance, contract, staffing, and people-operations work.", backstory="Your name is Cameron. You own HR and Legal only. You do not accept Marketing, Web Development, or IT ownership. Identify missing jurisdiction and professional-review needs; do not assume custom goods can always be non-refundable. " + rules, tools=[*search_tools, inspect_design_tool, critique_design_tool, generate_learned_design, approve_design_tool, list_references_tool, compare_reference_tool], **common)
    coo = Agent(tools=[inspect_design_tool, critique_design_tool, generate_learned_design, approve_design_tool, list_references_tool, compare_reference_tool], role="Morgan - Chief Operating Officer - Onyx and Ink", goal="Combine departmental recommendations into an actionable plan.", backstory="Your name is Morgan. You report to the CEO and prioritize departmental work, dependencies, owners, and decisions. " + rules, **common)
    from staff.model_routing import RoutedLLM
    if isinstance(llm,RoutedLLM):
        for name,agent in (('Avery',marketing),('Jordan',web),('Cameron',legal),('Morgan',coo)):agent.llm=llm.for_agent(name)
    if mail and mail.mode != "off":
        for name, agent in (("Avery", marketing), ("Jordan", web), ("Cameron", legal), ("Morgan", coo)):
            agent.tools.append(mail.tool_for(name,allow_owner=name=='Morgan'))
            agent.backstory += (
                f" Your email address is {mail.addresses[name]}. You may email James (CEO), Jaunee (Vice President), or named coworkers "
                "when a specific question, assignment, or decision needs their attention. "
                "Write internal email like a concise, friendly coworker. Use a natural subject, greeting, and plain language. Never use workflow jargon such as 'task handoff' or 'artifact'. "
                "Internal staff, James, and Jaunee email sends automatically. Email James only for an urgent risk, blocking decision, or one specific answer needed to complete useful work; keep it under 120 words and state the requested response clearly. The inbox monitor will route authenticated replies from James or Jaunee back to you for follow-up. "
                "Routine updates and completion reports stay in the dashboard; do not email them to James. "
                "A separate inbox monitor handles incoming replies. Do not claim to have received a reply unless it is supplied in your task context."
            )
    research = "Use web search for current claims." if search_key else "Web search is unavailable; clearly label market ideas as assumptions and list research needed."
    def task(agent, description, filename):
        return Task(
            description="CEO directive: " + directive + "\n\n" + description + " Work independently in your permanent department. Choose and finish one useful next action without waiting for another agent. Keep the report under 220 words. Do not invent numerical targets or operational facts; unsupported numbers must be omitted or labeled 'Proposed target — CEO approval required.' Return only a concise report using these exact labels, each followed by short hyphen bullets: Summary, Work completed, Assumptions and risks, Next actions, Decisions needed. Write 'None recorded' when a section has nothing to report. Never include thoughts, reasoning, tool narration, Markdown markers, tables, code fences, decorative separators, or 'Final Answer'.",
            expected_output="A complete concise report using Summary, Work completed, Assumptions and risks, Next actions, and Decisions needed; no reasoning transcript or code fence.",
            agent=agent,
            output_file="{report_dir}/"+filename,
        )
    tasks={
        'Morgan':task(coo,"Own COO coordination only. Review saved context, identify the highest-priority operational dependency, and complete one concrete coordination action. Do not reassign departments. Record any one genuine CEO decision needed.",'morgan-operations.md'),
        'Avery':task(marketing,"Your permanent department is Marketing only. Complete usable campaign copy, audience work, merchandising, research, product positioning, or channel material. Check sample inventory only as a marketing dependency. "+research,'avery-marketing.md'),
        'Jordan':task(web,"Your permanent department is Web Development and IT only. Produce a usable specification, acceptance criteria, content structure, integration plan, security review, or implementation-ready technical decision. Never claim code was deployed.",'jordan-web-it.md'),
        'Cameron':task(legal,"Your permanent department is HR and Legal only. Produce usable draft language or a focused, sourced policy, compliance, contract, staffing, or people-operations review. "+research,'cameron-hr-legal.md'),
    }
    for assignment in tasks.values():
        assignment.interpolate_inputs_and_add_conversation_history({'report_dir':str(report_dir)})
    filenames={'Morgan':'morgan-operations.md','Avery':'avery-marketing.md','Jordan':'jordan-web-it.md','Cameron':'cameron-hr-legal.md'}
    for name,assignment in tasks.items():
        mail_callback=mail.report_callback(name) if mail and mail.mode!='off' and name!='Morgan' else None
        def publish(output,notify=mail_callback):
            if assignment_output_valid(output) and notify:notify(output)
        assignment.callback=progress.callback(name,publish) if progress else publish
    return tasks


def run_assignments(tasks):
    """Run departments one at a time to preserve free-tier cloud capacity."""
    outputs={};errors=[];pending=iter(tasks.items());active={}
    with ThreadPoolExecutor(max_workers=1,thread_name_prefix='onyx-staff') as pool:
        for _ in range(1):
            try:name,task=next(pending);active[pool.submit(task.execute_sync)]=name
            except StopIteration:break
        halted=False
        while active:
            done,_=wait(active,return_when=FIRST_COMPLETED)
            for future in done:
                name=active.pop(future)
                try:output=future.result()
                except Exception as error:
                    release_staff_task(PROJECT_DIR,(tasks[name].assigned_task or {}).get('id'))
                    errors.append(error);halted=True;continue
                finalized=finalize_assignment_report(tasks[name].output_file,name,output)
                if finalized:
                    complete_staff_task(PROJECT_DIR,(tasks[name].assigned_task or {}).get('id'),output)
                else:
                    release_staff_task(PROJECT_DIR,(tasks[name].assigned_task or {}).get('id'))
                outputs[name]=output
            if halted:
                for future in active:future.cancel()
                # A request already in flight may finish; no new department starts.
                for future,name in list(active.items()):
                    if future.cancelled():continue
                    try:output=future.result()
                    except Exception as error:
                        release_staff_task(PROJECT_DIR,(tasks[name].assigned_task or {}).get('id'))
                        errors.append(error)
                    else:
                        finalized=finalize_assignment_report(tasks[name].output_file,name,output)
                        if finalized:complete_staff_task(PROJECT_DIR,(tasks[name].assigned_task or {}).get('id'),output)
                        else:release_staff_task(PROJECT_DIR,(tasks[name].assigned_task or {}).get('id'))
                        outputs[name]=output
                active.clear();break
            while len(active)<1:
                try:name,task=next(pending);active[pool.submit(task.execute_sync)]=name
                except StopIteration:break
    if errors:
        quota_errors=[error for error in errors if isinstance(error,APIStatusError) and error.status_code==429]
        if quota_errors:raise max(quota_errors,key=quota_retry_seconds)
        raise errors[0]
    return outputs


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run Onyx and Ink staff using Groq.")
    parser.add_argument("--check", action="store_true", help="Validate configuration without external API calls.")
    parser.add_argument("--directive", default=DEFAULT_DIRECTIVE)
    parser.add_argument("--assignment-id",default='')
    parser.add_argument("--project", default="", help="Project name or folder identifier for these assignments.")
    parser.add_argument("--verbose", action="store_true", help="Show detailed agent activity.")
    args = parser.parse_args(argv)
    load_dotenv(PROJECT_DIR / ".env")
    llm = None
    mail = None
    run_lock = None
    progress = None
    try:
        if not args.check:
            lock_path=PROJECT_DIR/'work'/'staff-run.lock';lock_path.parent.mkdir(exist_ok=True);run_lock=lock_path.open('a')
            try:fcntl.flock(run_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                print('Another staff run is already active. Wait for it to finish or stop autonomous staff in Settings.')
                return 1
        mail = StaffMail.from_env(PROJECT_DIR)
        search_key = credential("TAVILY_API_KEY", required=False)
        from staff.model_routing import RoutedLLM, routing_status
        cfg={**os.environ,**dotenv_values(PROJECT_DIR/'.env')}
        # Staff runs are background work.  The router keeps part of each provider's
        # minute budget available for CEO conversations in Telegram and the dashboard.
        # Autonomous reports are capped at 220 words.  Reserving a 1,000-token
        # completion on every free-tier route made otherwise healthy providers
        # reject the complete assignment context before any work could publish.
        llm=RoutedLLM(PROJECT_DIR,cfg,'Morgan',max_tokens=min(300,positive_int('GROQ_MAX_COMPLETION_TOKENS',1000)),priority='background')
        if not args.check:
            search_key = verified_search_key(search_key)
        assignment_id=args.assignment_id or datetime.now(BUSINESS_TZ).strftime('%Y%m%d-%H%M')+'-'+uuid.uuid4().hex[:6]
        progress=WorkProgress(PROJECT_DIR,assignment_id)
        project_id, project_name, project_folder = ensure_project(PROJECT_DIR, args.project) if args.project else active_project(PROJECT_DIR)
        consolidate_assignment_reports(PROJECT_DIR,project_id)
        report_dir = project_folder / 'agent-reports'
        report_dir.mkdir(parents=True, exist_ok=True)
        brief=project_context(PROJECT_DIR,project_id,limit=4000)
        tasks=build_assignments(llm,args.directive,report_dir,search_key,args.verbose,mail,progress=None if args.check else progress,project_brief=brief,root=PROJECT_DIR,project_id=project_id)
        if args.check:
            print(f"Setup OK: role-specific providers, four independent agents and four assignments. Web search: {'configured (not verified)' if search_key else 'disabled'}. Email: {mail.mode}. No API calls made; credentials are not verified.")
            print(json.dumps(routing_status(cfg)))
            return 0
        progress.write(detail='Morgan, Avery, Jordan, and Cameron are working independently.')
        print('Running four independent agents with role-specific cloud providers and shared usage budgets.')
        result = run_assignments(tasks)
        for report_path in report_dir.glob('*.md'):
            report_path.write_text(normalize_report(enforce_department_ownership(report_path.read_text()))+'\n')
        # Drive is a useful publishing target, not a dependency for staff work.
        # A disconnected Workspace must leave completed local reports intact and
        # must never be misreported as a cloud-model outage.
        consolidate_assignment_reports(PROJECT_DIR,project_id)
        try:
            from staff.report_records import export_consolidated_project
            export_consolidated_project(PROJECT_DIR,project_id)
            drive_detail=' The consolidated project report was updated in Google Drive.'
        except (RuntimeError, ValueError, OSError):
            drive_detail=' Google Drive is not connected, so the consolidated export is pending; local reports are saved.'
        mail.finish()
        progress.write(status='complete',detail='All four agents produced bounded draft deliverables. No unverified external action was claimed.'+drive_detail)
        print('\n'.join(f'{name}: {output}' for name,output in result.items()))
        print(f"Reports saved to {project_name}: {report_dir}")
        return 0
    except APIStatusError as error:
        retry_ready=None
        if error.status_code==429:
            retry_ready=save_provider_cooldown(PROJECT_DIR,quota_retry_seconds(error))
            resume=datetime.fromtimestamp(retry_ready, BUSINESS_TZ).strftime('%-I:%M %p')
            if progress:progress.write(status='waiting',detail='Groq usage limit reached. Autonomous work will resume automatically around '+resume+'.')
        elif progress:progress.write(status='failed',detail='Groq stopped one or more assignments before completion.')
        messages = {401: "Groq rejected the API key. Check GROQ_API_KEY.", 403: "Groq denied model access. Check model permissions in your Groq account.", 404: "Groq model not found. Check GROQ_MODEL.", 429: "Groq usage capacity is exhausted. Autonomous staff will resume at the provider reset time.", 400: "Groq rejected a request. Check model compatibility and the prompt size."}
        print(messages.get(error.status_code, f"Groq returned HTTP {error.status_code} after retries. Try later."))
        if mail and not args.check:
            if retry_ready:
                mail.failure('Groq reached its usage limit. Autonomous staff paused and will resume automatically around '+datetime.fromtimestamp(retry_ready, BUSINESS_TZ).strftime('%-I:%M %p')+'.','Provider HTTP 429; the provider supplied a retry window.')
            else:mail.failure("A cloud provider rejected the staff run.","Provider HTTP "+str(error.status_code))
        return 1
    except (APIConnectionError, ConnectionError):
        if progress:progress.write(status='failed',detail='The AI service could not be reached.')
        print("Could not reach Groq after retries. Check your connection and try later.")
        if mail and not args.check:
            mail.failure("A cloud provider could not be reached.","Network connection to the cloud provider failed.")
        return 1
    except RoutingError as error:
        if progress:
            progress.agents={name:'waiting' for name in progress.agents}
            progress.write(status='waiting',detail=str(error)+' Retrying automatically while staff hours are active.')
        print(str(error));return 1
    except ValueError as error:
        if progress:progress.write(status='failed',detail='Configuration needs attention.')
        print(f"Configuration/request error: {error}")
        if mail and not args.check:
            mail.failure("Autonomous staff needs configuration attention.",str(error))
        return 1
    except Exception:
        if progress:progress.write(status='failed',detail='Independent staff work stopped unexpectedly.')
        if mail and not args.check:
            mail.failure("Autonomous staff stopped unexpectedly.","Unexpected worker exception; see Dashboard Activity for status.")
        raise
    finally:
        if llm:
            llm.close()
        if run_lock:
            fcntl.flock(run_lock,fcntl.LOCK_UN);run_lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
