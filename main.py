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
from groq_llm import GroqLLM
from model_routing import RoutingError
from staff_email import StaffMail
from report_format import normalize_report
from report_projects import active_project, ensure_project, consolidate_assignment_reports, project_context
from staff_tasks import claim as claim_staff_task, complete as complete_staff_task, release as release_staff_task
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


def build_assignments(llm, directive, report_dir, search_key=None, verbose=False, mail=None, progress=None, project_brief='', root=None, project_id=''):
    # CrewAI adds a large control prompt and multi-turn loop which the free cloud
    # routes reject before publishing a report. Autonomous work deliberately uses
    # a single bounded request; interactive staff retain their tool-capable flow.
    if isinstance(llm, __import__('model_routing').RoutedLLM):
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
    marketing = Agent(role="Avery - Marketing - Onyx and Ink", goal="Complete marketing, brand, campaign, audience, merchandising, and customer messaging work.", backstory="Your name is Avery. You own Marketing only. You do not accept Legal, HR, Web Development, or IT ownership. You oversee customizable tumblers, shirts, keychains, puzzles, and bookmarks from the marketing perspective. " + rules, tools=[check_blank_stock, *search_tools], **common)
    web = Agent(role="Jordan - Web Development and IT - Onyx and Ink", goal="Complete website, storefront, dashboard, integration, infrastructure, security, and technical specification work.", backstory="Your name is Jordan. You own Web Development and IT only. You do not accept Marketing, HR, or Legal ownership. You prepare implementation requirements for the storefront and internal systems. " + rules, **common)
    legal = Agent(role="Cameron - HR and Legal - Onyx and Ink", goal="Complete HR, policy, compliance, contract, staffing, and people-operations work.", backstory="Your name is Cameron. You own HR and Legal only. You do not accept Marketing, Web Development, or IT ownership. Identify missing jurisdiction and professional-review needs; do not assume custom goods can always be non-refundable. " + rules, tools=search_tools, **common)
    coo = Agent(role="Morgan - Chief Operating Officer - Onyx and Ink", goal="Combine departmental recommendations into an actionable plan.", backstory="Your name is Morgan. You report to the CEO and prioritize departmental work, dependencies, owners, and decisions. " + rules, **common)
    from model_routing import RoutedLLM
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
            description="CEO directive: " + directive + "\n\n" + description + " Work independently in your permanent department. Choose and finish one useful next action without waiting for another agent. Keep the report under 220 words. Do not invent numerical targets or operational facts; unsupported numbers must be omitted or labeled 'Proposed target — CEO approval required.' Return only the finished plain-text report. Use brief title-style lines and hyphen bullets; no Markdown markers, tables, code fences, or decorative separators. Never include thoughts, reasoning, tool narration, or 'Final Answer'.",
            expected_output="A complete, concise plain-text report with actions, assumptions, and open decisions; no reasoning transcript, Markdown, or code fence.",
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
        from model_routing import RoutedLLM, routing_status
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
            from report_records import export_consolidated_project
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
