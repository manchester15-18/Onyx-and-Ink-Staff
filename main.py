"""Run the Onyx and Ink staff on Groq. Configuration lives in .env."""
import argparse
import fcntl
import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

# Keep CrewAI runtime data inside this project, before importing CrewAI.
PROJECT_DIR = Path(__file__).resolve().parent
os.environ.setdefault("CREWAI_STORAGE_DIR", str(PROJECT_DIR / "work" / "crewai-storage"))
os.environ["CREWAI_TELEMETRY_DISABLED"] = "true"
os.environ["CREWAI_TRACING_ENABLED"] = "false"
os.environ.setdefault("OTEL_SDK_DISABLED", "true")


from dotenv import load_dotenv
from crewai import Agent, Crew, Process, Task
from crewai.tools import tool
from openai import APIConnectionError, APIStatusError
from groq_llm import GroqLLM
from staff_email import StaffMail
from report_format import normalize_report
from web_research import WebResearchError, search as tavily_search, verify as verify_tavily

DEFAULT_DIRECTIVE = "Plan our upcoming custom gift product push for Onyx and Ink. Provide a coordinated operational plan."
SAMPLE_INVENTORY = {
    "tumbler": "240 units (20oz Stainless Steel - White)",
    "keychain": "500 units (Acrylic Clear Blanks)",
    "shirt": "150 units (Heavy Cotton Crewneck - Black)",
    "puzzle": "85 units (120-piece Sublimation Blanks)",
    "bookmark": "300 units (Aluminum Gloss)",
}


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


class CycleProgress:
    """Private progress receipt used by the dashboard; it contains no credentials."""
    def __init__(self,root,cycle_id):
        self.path=Path(root)/'work'/'staff-cycle-status.json';self.cycle_id=cycle_id;self.completed=[];self.lock=threading.Lock()
    def write(self,phase,status='running',detail=''):
        with self.lock:
            data={'cycle':self.cycle_id,'phase':phase,'status':status,'detail':detail,'completed':list(self.completed),'updated':datetime.now().astimezone().isoformat(timespec='seconds')}
            self.path.parent.mkdir(exist_ok=True);temp=self.path.with_suffix('.tmp');temp.write_text(json.dumps(data,indent=2));temp.chmod(0o600);temp.replace(self.path)
    def callback(self,name,after=None,next_phase=''):
        def done(output):
            if after:after(output)
            with self.lock:
                if name not in self.completed:self.completed.append(name)
            self.write(next_phase or name+' completed',detail=name+' finished its assigned work.')
        return done


def build_crew(llm, search_key=None, verbose=False, mail=None, progress=None):
    search_tools = [search_web] if search_key else []
    rules = (
        "Use one JSON object matching the schema per tool call; never a top-level array. "
        "Keep tool calls and reports concise. Distinguish facts, sample inventory, assumptions, and recommendations. "
        "Never invent numeric targets, budgets, deadlines, capacity, conversion rates, revenue, inventory, or performance results. Use a number only when it comes from the CEO directive, the sample inventory tool, or a cited current source. Otherwise say the value is unknown or label it 'Proposed target — CEO approval required.' "
        "Cite URLs for researched claims. Never invent search results or claim changes were deployed. "
        "Start the work immediately. Choose and complete a concrete useful next step with the tools available; do not wait for another agent or an email unless a decision truly blocks progress. "
        "Always return a visible CrewAI Thought/Action instruction or Final Answer; never return reasoning-only output. "
        "If web search reports unavailable, do not retry it; continue with clearly labeled assumptions."
    )
    common = dict(llm=llm, allow_delegation=False, max_retry_limit=0, max_iter=6, verbose=verbose)
    marketing = Agent(role="Avery - Head of Marketing - Onyx and Ink", goal="Plan a focused custom gift campaign.", backstory="Your name is Avery. You oversee customizable tumblers, shirts, keychains, puzzles, and bookmarks. " + rules, tools=[check_blank_stock, *search_tools], **common)
    web = Agent(role="Jordan - Head of Web Development - Onyx and Ink", goal="Specify product personalization and checkout improvements.", backstory="Your name is Jordan. You prepare implementation requirements for the storefront. " + rules, **common)
    legal = Agent(role="Cameron - Head of Legal and HR - Onyx and Ink", goal="Draft customer policies and operational review items.", backstory="Your name is Cameron. You prepare policy drafts. Identify missing jurisdiction and legal review needs; do not assume custom goods can always be non-refundable. " + rules, tools=search_tools, **common)
    coo = Agent(role="Morgan - Chief Operating Officer - Onyx and Ink", goal="Combine departmental recommendations into an actionable plan.", backstory="Your name is Morgan. You report to the CEO and prioritize departmental work, dependencies, owners, and decisions. " + rules, **common)
    if mail and mail.mode != "off":
        for name, agent in (("Avery", marketing), ("Jordan", web), ("Cameron", legal), ("Morgan", coo)):
            agent.tools.append(mail.tool_for(name,allow_owner=name=='Morgan'))
            agent.backstory += (
                f" Your email address is {mail.addresses[name]}. You may email Owner or named coworkers "
                "when a specific question, assignment, or decision needs their attention. "
                "Write internal email like a concise, friendly coworker. Use a natural subject, greeting, and plain language. Never use workflow jargon such as 'task handoff', 'artifact', or 'execution cycle'. "
                "Internal staff and Owner email sends automatically. Email Owner only for an urgent risk, blocking decision, or one specific answer needed to complete useful work; keep it under 120 words and state the requested response clearly. The inbox monitor will route the authenticated reply back to you for follow-up. "
                "Routine updates and completion reports stay in the dashboard; do not email them to Owner. "
                "A separate inbox monitor handles incoming replies. Do not claim to have received a reply unless it is supplied in your task context."
            )
    research = "Use web search for current claims." if search_key else "Web search is unavailable; clearly label market ideas as assumptions and list research needed."
    def task(agent, description, filename, context=None):
        return Task(
            description="CEO directive: {directive}\n\n" + description + " Keep the report under 220 words. Do not invent numerical targets or operational facts; unsupported numbers must be omitted or labeled 'Proposed target — CEO approval required.' Return only the finished Markdown report. Never include thoughts, reasoning, tool narration, 'Final Answer', or code fences.",
            expected_output="A complete, concise Markdown report with actions, assumptions, and open decisions; no reasoning transcript or code fence.",
            agent=agent,
            context=context or [],
            # Template interpolation preserves absolute paths in CrewAI 1.6.1.
            output_file="{report_dir}/" + filename,
        )
    brief = task(coo, "Start the cycle by reviewing the saved context. Define one concrete, finishable outcome for Avery, Jordan, and Cameron that advances the objective without repeating completed work. Keep nonblocking work moving even when a CEO decision is pending. Consolidate all genuine CEO questions into at most one section, but do not email the CEO during kickoff. Assign only work possible with the agents' stated tools; distinguish implementation from recommendations.", "cycle_brief.md")
    marketing_task = task(marketing, "Use Morgan's cycle brief as your assignment. Complete the marketing outcome now rather than drafting another broad plan. Check sample blank inventory when relevant, then produce usable campaign copy, product decisions, research, or channel material. State evidence, exactly what changed, and the next owner. " + research, "marketing_campaign.md", [brief])
    web_task = task(web, "Use Morgan's cycle brief as your assignment. Complete the storefront outcome now rather than repeating general requirements. Produce a usable specification, acceptance criteria, content structure, or implementation-ready decision within your available tools. Never claim code was deployed. State evidence, exactly what changed, and the next owner.", "web_dev_specs.md", [brief])
    legal_task = task(legal, "Use Morgan's cycle brief as your assignment. Complete the policy or HR outcome now rather than repeating general advice. Produce usable draft language or a focused, sourced review and flag only a decision that truly blocks further work. State evidence, exactly what changed, and the next owner. " + research, "legal_terms.md", [brief])
    marketing_task.async_execution=True
    web_task.async_execution=True
    legal_task.async_execution=True
    summary = task(coo, "Review the kickoff brief and the three completed department updates. Record completed work separately from recommendations, resolve overlaps, assign the next executable owners, and carry forward unfinished items. Email the CEO only if one consolidated response is genuinely required before useful work can continue.", "operational_plan.md", [brief,marketing_task, web_task, legal_task])
    if mail and mail.mode != "off":
        for name, staff_task in (("Avery", marketing_task), ("Jordan", web_task), ("Cameron", legal_task)):
            callback=mail.report_callback(name)
            staff_task.callback=progress.callback(name,callback,'Departments working in parallel') if progress else callback
    if progress:
        brief.callback=progress.callback('Morgan kickoff',next_phase='Departments working in parallel')
        summary.callback=progress.callback('Morgan review',next_phase='Cycle complete')
    return Crew(tracing=False, agents=[marketing, web, legal, coo], tasks=[brief,marketing_task, web_task, legal_task, summary], process=Process.sequential, verbose=verbose)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run Onyx and Ink staff using Groq.")
    parser.add_argument("--check", action="store_true", help="Validate configuration without external API calls.")
    parser.add_argument("--directive", default=DEFAULT_DIRECTIVE)
    parser.add_argument("--cycle-id",default='')
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
        )
        if not args.check:
            search_key = verified_search_key(search_key)
        cycle_id=args.cycle_id or datetime.now().strftime('%Y%m%d-%H%M')+'-'+uuid.uuid4().hex[:6]
        progress=CycleProgress(PROJECT_DIR,cycle_id)
        crew = build_crew(llm, search_key, args.verbose, mail,progress=None if args.check else progress)
        inputs = {"directive": args.directive, "report_dir": str(PROJECT_DIR / "reports")}
        if args.check:
            crew._interpolate_inputs(inputs)
            print(f"Setup OK: Groq model {llm.model}, four agents, five phased tasks. Web search: {'configured (not verified)' if search_key else 'disabled'}. Email: {mail.mode}. No API calls made; key validity is not checked.")
            return 0
        progress.write('Morgan prioritizing',detail='Morgan is reviewing prior work and assigning this cycle.')
        print(f"Running Onyx and Ink staff on Groq ({llm.model}). Free-tier pacing may pause between requests.")
        result = crew.kickoff(inputs=inputs)
        for report_path in (PROJECT_DIR/'reports').glob('*.md'):
            report_path.write_text(normalize_report(report_path.read_text())+'\n')
        mail.finish()
        progress.write('Cycle complete',status='complete',detail='Morgan completed the cycle review.')
        print(result)
        print(f"Reports saved to {PROJECT_DIR / 'reports'}")
        return 0
    except APIStatusError as error:
        if progress:progress.write('Cycle stopped',status='failed',detail='Groq stopped the cycle before completion.')
        messages = {401: "Groq rejected the API key. Check GROQ_API_KEY.", 403: "Groq denied model access. Check model permissions in your Groq account.", 404: "Groq model not found. Check GROQ_MODEL.", 429: "Groq quota is exhausted after bounded retries. Check your account's request, token, and daily limits.", 400: "Groq rejected a request. Check model compatibility and the prompt size."}
        print(messages.get(error.status_code, f"Groq returned HTTP {error.status_code} after retries. Try later."))
        if mail and not args.check:
            mail.failure("The staff run stopped. Check the local run output for the configuration or service issue.")
        return 1
    except (APIConnectionError, ConnectionError):
        if progress:progress.write('Cycle stopped',status='failed',detail='The AI service could not be reached.')
        print("Could not reach Groq after retries. Check your connection and try later.")
        if mail and not args.check:
            mail.failure("The staff run stopped. Check the local run output for the configuration or service issue.")
        return 1
    except ValueError as error:
        if progress:progress.write('Cycle stopped',status='failed',detail='Configuration needs attention.')
        print(f"Configuration/request error: {error}")
        if mail and not args.check:
            mail.failure("The staff run stopped due to a configuration or request issue. Check local output.")
        return 1
    except Exception:
        if progress:progress.write('Cycle stopped',status='failed',detail='The cycle stopped unexpectedly.')
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
