"""Run the Onyx and Ink staff on Groq. Configuration lives in .env."""
import argparse
import fcntl
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path

# Keep CrewAI runtime data inside this project, before importing CrewAI.
PROJECT_DIR = Path(__file__).resolve().parent
os.environ.setdefault("CREWAI_STORAGE_DIR", str(PROJECT_DIR / "work" / "crewai-storage"))
os.environ["CREWAI_TELEMETRY_DISABLED"] = "true"
os.environ["CREWAI_TRACING_ENABLED"] = "false"
os.environ.setdefault("OTEL_SDK_DISABLED", "true")


from dotenv import load_dotenv
from crewai import Agent, Task
from crewai.tools import tool
from openai import APIConnectionError, APIStatusError
from groq_llm import GroqLLM
from staff_email import StaffMail
from report_format import normalize_report
from report_projects import active_project, ensure_project
from web_research import WebResearchError, search as tavily_search, verify as verify_tavily

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
        cleaned=normalize_report(enforce_department_ownership(raw))
        cleaned=re.sub(r'(?i)\bcampaign cycle\b','campaign',cleaned)
        cleaned=re.sub(r'(?i)\bwork cycle\b','assignment',cleaned)
    path.write_text(cleaned.rstrip()+'\n');path.chmod(0o600)
    return not invalid and bool(raw.strip())


class WorkProgress:
    """Private per-agent work status used by the dashboard."""
    def __init__(self,root,assignment_id):
        self.path=Path(root)/'work'/'staff-work-status.json';self.assignment_id=assignment_id;self.lock=threading.Lock()
        self.agents={name:'working' for name in ('Morgan','Avery','Jordan','Cameron')}
    def write(self,status='working',detail='Staff are completing independent assignments.'):
        with self.lock:
            data={'assignment':self.assignment_id,'status':status,'detail':detail,'agents':dict(self.agents),'updated':datetime.now().astimezone().isoformat(timespec='seconds')}
            self.path.parent.mkdir(exist_ok=True);temp=self.path.with_suffix('.tmp');temp.write_text(json.dumps(data,indent=2));temp.chmod(0o600);temp.replace(self.path)
    def callback(self,name,after=None):
        def done(output):
            if after:after(output)
            with self.lock:self.agents[name]='complete'
            self.write(detail=name+' completed an assignment; other staff continue independently.')
        return done


def build_assignments(llm, directive, report_dir, search_key=None, verbose=False, mail=None, progress=None):
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
    common = dict(llm=llm, allow_delegation=False, max_retry_limit=0, max_iter=3, verbose=verbose)
    marketing = Agent(role="Avery - Marketing - Onyx and Ink", goal="Complete marketing, brand, campaign, audience, merchandising, and customer messaging work.", backstory="Your name is Avery. You own Marketing only. You do not accept Legal, HR, Web Development, or IT ownership. You oversee customizable tumblers, shirts, keychains, puzzles, and bookmarks from the marketing perspective. " + rules, tools=[check_blank_stock, *search_tools], **common)
    web = Agent(role="Jordan - Web Development and IT - Onyx and Ink", goal="Complete website, storefront, dashboard, integration, infrastructure, security, and technical specification work.", backstory="Your name is Jordan. You own Web Development and IT only. You do not accept Marketing, HR, or Legal ownership. You prepare implementation requirements for the storefront and internal systems. " + rules, **common)
    legal = Agent(role="Cameron - HR and Legal - Onyx and Ink", goal="Complete HR, policy, compliance, contract, staffing, and people-operations work.", backstory="Your name is Cameron. You own HR and Legal only. You do not accept Marketing, Web Development, or IT ownership. Identify missing jurisdiction and professional-review needs; do not assume custom goods can always be non-refundable. " + rules, tools=search_tools, **common)
    coo = Agent(role="Morgan - Chief Operating Officer - Onyx and Ink", goal="Combine departmental recommendations into an actionable plan.", backstory="Your name is Morgan. You report to the CEO and prioritize departmental work, dependencies, owners, and decisions. " + rules, **common)
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
            description="CEO directive: " + directive + "\n\n" + description + " Work independently in your permanent department. Choose and finish one useful next action without waiting for another agent. Keep the report under 220 words. Do not invent numerical targets or operational facts; unsupported numbers must be omitted or labeled 'Proposed target — CEO approval required.' Return only the finished Markdown report. Never include thoughts, reasoning, tool narration, 'Final Answer', or code fences.",
            expected_output="A complete, concise Markdown report with actions, assumptions, and open decisions; no reasoning transcript or code fence.",
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
    """Run independent departments two at a time so one quota error stops new launches."""
    outputs={};errors=[];pending=iter(tasks.items());active={}
    with ThreadPoolExecutor(max_workers=2,thread_name_prefix='onyx-staff') as pool:
        for _ in range(2):
            try:name,task=next(pending);active[pool.submit(task.execute_sync)]=name
            except StopIteration:break
        halted=False
        while active:
            done,_=wait(active,return_when=FIRST_COMPLETED)
            for future in done:
                name=active.pop(future)
                try:output=future.result()
                except Exception as error:
                    errors.append(error);halted=True;continue
                finalize_assignment_report(tasks[name].output_file,name,output)
                from report_records import auto_export_reference
                auto_export_reference(PROJECT_DIR,tasks[name].output_file)
                outputs[name]=output
            if halted:
                for future in active:future.cancel()
                # A request already in flight may finish; no new department starts.
                for future,name in list(active.items()):
                    if future.cancelled():continue
                    try:output=future.result()
                    except Exception as error:errors.append(error)
                    else:
                        finalize_assignment_report(tasks[name].output_file,name,output);outputs[name]=output
                active.clear();break
            while len(active)<2:
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
        key = credential("GROQ_API_KEY")
        search_key = credential("TAVILY_API_KEY", required=False)
        llm = GroqLLM(
            key, model=os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b"),
            rpm=positive_int("GROQ_RPM", 25), tpm=positive_int("GROQ_TPM", 8000),
            max_tokens=positive_int("GROQ_MAX_COMPLETION_TOKENS", 1000),
            max_retries=0,
        )
        if not args.check:
            search_key = verified_search_key(search_key)
        assignment_id=args.assignment_id or datetime.now().strftime('%Y%m%d-%H%M')+'-'+uuid.uuid4().hex[:6]
        progress=WorkProgress(PROJECT_DIR,assignment_id)
        project_id, project_name, project_folder = ensure_project(PROJECT_DIR, args.project) if args.project else active_project(PROJECT_DIR)
        report_dir = project_folder / "assignments" / assignment_id
        report_dir.mkdir(parents=True, exist_ok=True)
        tasks=build_assignments(llm,args.directive,report_dir,search_key,args.verbose,mail,progress=None if args.check else progress)
        if args.check:
            print(f"Setup OK: Groq model {llm.model}, four independent agents and four assignments. Web search: {'configured (not verified)' if search_key else 'disabled'}. Email: {mail.mode}. No API calls made; key validity is not checked.")
            return 0
        progress.write(detail='Morgan, Avery, Jordan, and Cameron are working independently.')
        print(f"Running four independent Onyx and Ink agents on Groq ({llm.model}). Shared pacing may pause requests to protect usage limits.")
        result = run_assignments(tasks)
        for report_path in report_dir.glob('*.md'):
            report_path.write_text(normalize_report(enforce_department_ownership(report_path.read_text()))+'\n')
        mail.finish()
        progress.write(status='complete',detail='All four agents completed an assignment; continuous work may start the next assignments.')
        print('\n'.join(f'{name}: {output}' for name,output in result.items()))
        print(f"Reports saved to {project_name}: {report_dir}")
        return 0
    except APIStatusError as error:
        retry_ready=None
        if error.status_code==429:
            retry_ready=save_provider_cooldown(PROJECT_DIR,quota_retry_seconds(error))
            resume=datetime.fromtimestamp(retry_ready).astimezone().strftime('%-I:%M %p')
            if progress:progress.write(status='waiting',detail='Groq usage limit reached. Autonomous work will resume automatically around '+resume+'.')
        elif progress:progress.write(status='failed',detail='Groq stopped one or more assignments before completion.')
        messages = {401: "Groq rejected the API key. Check GROQ_API_KEY.", 403: "Groq denied model access. Check model permissions in your Groq account.", 404: "Groq model not found. Check GROQ_MODEL.", 429: "Groq usage capacity is exhausted. Autonomous staff will resume at the provider reset time.", 400: "Groq rejected a request. Check model compatibility and the prompt size."}
        print(messages.get(error.status_code, f"Groq returned HTTP {error.status_code} after retries. Try later."))
        if mail and not args.check:
            if retry_ready:
                mail.failure('Groq reached its usage limit. Autonomous staff paused and will resume automatically around '+datetime.fromtimestamp(retry_ready).astimezone().strftime('%-I:%M %p')+'.')
            else:mail.failure("The staff run stopped. Check the local run output for the configuration or service issue.")
        return 1
    except (APIConnectionError, ConnectionError):
        if progress:progress.write(status='failed',detail='The AI service could not be reached.')
        print("Could not reach Groq after retries. Check your connection and try later.")
        if mail and not args.check:
            mail.failure("The staff run stopped. Check the local run output for the configuration or service issue.")
        return 1
    except ValueError as error:
        if progress:progress.write(status='failed',detail='Configuration needs attention.')
        print(f"Configuration/request error: {error}")
        if mail and not args.check:
            mail.failure("The staff run stopped due to a configuration or request issue. Check local output.")
        return 1
    except Exception:
        if progress:progress.write(status='failed',detail='Independent staff work stopped unexpectedly.')
        if mail and not args.check:
            mail.failure("The staff run stopped unexpectedly. Check the local run output.")
        raise
    finally:
        if llm:
            llm.close()
        if run_lock:
            fcntl.flock(run_lock,fcntl.LOCK_UN);run_lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
