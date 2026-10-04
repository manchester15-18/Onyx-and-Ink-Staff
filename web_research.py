"""Tavily-backed web research with bounded, citation-friendly output."""
import json
from pathlib import Path

import requests
from dotenv import dotenv_values

TAVILY_SEARCH_URL = "https://api.tavily.com/search"


class WebResearchError(RuntimeError):
    pass


def _request(api_key, query, max_results=5, timeout=30):
    if not api_key:
        raise WebResearchError("Tavily is not configured.")
    try:
        response = requests.post(
            TAVILY_SEARCH_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "query": query,
                "topic": "general",
                "search_depth": "basic",
                "max_results": max(1, min(int(max_results), 8)),
                "include_answer": False,
                "include_raw_content": False,
            },
            timeout=timeout,
        )
    except requests.RequestException as error:
        raise WebResearchError("Tavily could not be reached.") from error
    if response.status_code in (401, 403):
        raise WebResearchError("Tavily rejected the API key.")
    if response.status_code == 429:
        raise WebResearchError("Tavily search credits or rate limit are exhausted.")
    if not response.ok:
        raise WebResearchError(f"Tavily returned HTTP {response.status_code}.")
    try:
        return response.json()
    except ValueError as error:
        raise WebResearchError("Tavily returned an invalid response.") from error


def search(project_dir, query, max_results=5):
    query = str(query).strip()
    if not query or len(query) > 500:
        raise ValueError("Provide a web-search query under 500 characters.")
    api_key = dotenv_values(Path(project_dir) / ".env").get("TAVILY_API_KEY")
    data = _request(api_key, query, max_results=max_results)
    results = []
    for item in data.get("results", [])[:max_results]:
        url = str(item.get("url", ""))[:1500]
        if not url.startswith(("https://", "http://")):
            continue
        results.append({
            "title": str(item.get("title", ""))[:300],
            "url": url,
            "content": str(item.get("content", ""))[:1800],
        })
    if not results:
        return "No Tavily results were found. Continue with clearly labeled assumptions and do not retry the same search."
    return json.dumps({"query": query, "results": results}, ensure_ascii=False)


def verify(api_key):
    _request(api_key, "Onyx and Ink custom gifts", max_results=1, timeout=15)
    return True
