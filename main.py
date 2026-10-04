"""Run the Onyx and Ink staff on Groq. Configuration lives in .env."""
import argparse
import os
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
        normalized = name.strip().lower()
        normalized = aliases.get(normalized, normalized)
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


def build_crew(llm, search_key=None, verbose=False, mail=None):
    search_tools = [search_web] if search_key else []
    rules = (
        "Use one JSON object matching the schema per tool call; never a top-level array. "
        "Keep tool calls and reports concise. Distinguish facts, sample inventory, assumptions, and recommendations. "
        "Cite URLs for researched claims. Never invent search results or claim changes were deployed. "
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
            agent.tools.append(mail.tool_for(name))
            agent.backstory += (
                f" Your email address is {mail.addresses[name]}. You may email Owner or named coworkers "
                "when a specific question, assignment, or decision needs their attention. "
                "Internal staff and Owner email sends automatically. Email Owner only for an urgent risk, blocking decision, or one specific answer needed to complete useful work; keep it under 120 words and state the requested response clearly. The inbox monitor will route the authenticated reply back to you for follow-up. "
                "Routine updates and completion reports stay in the dashboard; do not email them to Owner. "
                "A separate inbox monitor handles incoming replies. Do not claim to have received a reply unless it is supplied in your task context."
            )
    research = "Use web search for current claims." if search_key else "Web search is unavailable; clearly label market ideas as assumptions and list research needed."
    def task(agent, description, filename, context=None):
        return Task(
            description="CEO directive: {directive}\n\n" + description + " Keep the report under 300 words.",
            expected_output="A concise Markdown report with actions, assumptions, and open decisions.",
            agent=agent,
            context=context or [],
            # Template interpolation preserves absolute paths in CrewAI 1.6.1.
            output_file="{report_dir}/" + filename,
        )
    marketing_task = task(marketing, "Check sample blank inventory and propose products, audiences, offers, channels, and campaign measures. " + research, "marketing_campaign.md")
    web_task = task(web, "Specify live text/image preview, upload validation, accessibility, mobile checkout, and implementation acceptance criteria.", "web_dev_specs.md")
    legal_task = task(legal, "Draft custom-order approval, returns, defects, IP permissions, and dispute terms. Flag jurisdiction-specific questions. " + research, "legal_terms.md")
    summary = task(coo, "Combine the three supplied department reports into priorities, owners, dependencies, success measures, and CEO decisions. Preserve uncertainties and sample-data labels.", "operational_plan.md", [marketing_task, web_task, legal_task])
    if mail and mail.mode != "off":
        for name, staff_task in (("Avery", marketing_task), ("Jordan", web_task), ("Cameron", legal_task), ("Morgan", summary)):
            staff_task.callback = mail.report_callback(name)
    return Crew(tracing=False, agents=[marketing, web, legal, coo], tasks=[marketing_task, web_task, legal_task, summary], process=Process.sequential, verbose=verbose)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run Onyx and Ink staff using Groq.")
    parser.add_argument("--check", action="store_true", help="Validate configuration without external API calls.")
    parser.add_argument("--directive", default=DEFAULT_DIRECTIVE)
    parser.add_argument("--verbose", action="store_true", help="Show detailed agent activity.")
    args = parser.parse_args(argv)
    load_dotenv(PROJECT_DIR / ".env")
    llm = None
    mail = None
    try:
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
        crew = build_crew(llm, search_key, args.verbose, mail)
        inputs = {"directive": args.directive, "report_dir": str(PROJECT_DIR / "reports")}
        if args.check:
            crew._interpolate_inputs(inputs)
            print(f"Setup OK: Groq model {llm.model}, four agents, four tasks. Web search: {'configured (not verified)' if search_key else 'disabled'}. Email: {mail.mode}. No API calls made; key validity is not checked.")
            return 0
        print(f"Running Onyx and Ink staff on Groq ({llm.model}). Free-tier pacing may pause between requests.")
        result = crew.kickoff(inputs=inputs)
        mail.finish()
        print(result)
        print(f"Reports saved to {PROJECT_DIR / 'reports'}")
        return 0
    except APIStatusError as error:
        messages = {401: "Groq rejected the API key. Check GROQ_API_KEY.", 403: "Groq denied model access. Check model permissions in your Groq account.", 404: "Groq model not found. Check GROQ_MODEL.", 429: "Groq quota is exhausted after bounded retries. Check your account's request, token, and daily limits.", 400: "Groq rejected a request. Check model compatibility and the prompt size."}
        print(messages.get(error.status_code, f"Groq returned HTTP {error.status_code} after retries. Try later."))
        if mail and not args.check:
            mail.failure("The staff run stopped. Check the local run output for the configuration or service issue.")
        return 1
    except (APIConnectionError, ConnectionError):
        print("Could not reach Groq after retries. Check your connection and try later.")
        if mail and not args.check:
            mail.failure("The staff run stopped. Check the local run output for the configuration or service issue.")
        return 1
    except ValueError as error:
        print(f"Configuration/request error: {error}")
        if mail and not args.check:
            mail.failure("The staff run stopped due to a configuration or request issue. Check local output.")
        return 1
    except Exception:
        if mail and not args.check:
            mail.failure("The staff run stopped unexpectedly. Check the local run output.")
        raise
    finally:
        if llm:
            llm.close()


if __name__ == "__main__":
    raise SystemExit(main())
