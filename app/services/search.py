"""Live web research via Tavily.

Used to ground the platform in real Nigerian education data (subject
requirements for a course, exam dates, target cut-off marks) rather than letting
an LLM hallucinate them. Provider is abstracted behind ``web_search`` so we can
swap Tavily -> Serper later via ``SEARCH_PROVIDER``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("studymaster.search")

TAVILY_URL = "https://api.tavily.com/search"


async def web_search(
    query: str,
    *,
    max_results: int = 5,
    include_answer: bool = True,
    search_depth: str = "advanced",
) -> dict[str, Any]:
    """Run a web search and return ``{answer, results:[{title,url,content}], query}``.

    Never raises into the request path — returns an empty result set on failure so
    callers can degrade gracefully (e.g. fall back to LLM-only reasoning).
    """
    if not settings.search_api_key:
        logger.warning("SEARCH_API_KEY not set; returning empty search result.")
        return {"answer": "", "results": [], "query": query}

    if settings.search_provider != "tavily":
        logger.warning("Unsupported SEARCH_PROVIDER=%s", settings.search_provider)
        return {"answer": "", "results": [], "query": query}

    payload = {
        "api_key": settings.search_api_key,
        "query": query,
        "max_results": max_results,
        "include_answer": include_answer,
        "search_depth": search_depth,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(TAVILY_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Tavily search failed for %r: %s", query, exc)
        return {"answer": "", "results": [], "query": query}

    results = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
        }
        for r in data.get("results", [])
    ]
    return {"answer": data.get("answer", ""), "results": results, "query": query}


def format_results_for_llm(search: dict[str, Any]) -> str:
    """Compact, citation-friendly rendering of search results for an LLM prompt."""
    lines: list[str] = []
    if search.get("answer"):
        lines.append(f"Summary: {search['answer']}")
    for i, r in enumerate(search.get("results", []), 1):
        lines.append(f"[{i}] {r['title']} ({r['url']})\n{r['content']}")
    return "\n\n".join(lines) if lines else "No web results found."
