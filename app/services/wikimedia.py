"""Wikimedia Commons image search.

Finds openly-licensed images to illustrate notes. Returns thumbnail URLs that
can be hot-linked directly (upload.wikimedia.org allows this), plus title +
description so the agent can verify relevance and so read-aloud/live-teaching can
describe each image.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

logger = logging.getLogger("studymaster.wikimedia")

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia's API policy asks for an identifying User-Agent with a contact URL;
# generic agents are throttled much harder (we hit 429s with the old one).
USER_AGENT = "StudyMaster/2.0 (https://studymaster.live; educational image lookup) httpx"

# Same searches repeat across lessons ("amoeba", "plant cell"): cache them so a
# busy day does not hammer Wikimedia. In-process, bounded, ~1 day.
_CACHE: dict[tuple, tuple[float, list]] = {}
_CACHE_TTL = 24 * 3600
_CACHE_MAX = 500


def _cached(key: tuple) -> list | None:
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL:
        return hit[1]
    return None


def _store(key: tuple, value: list) -> list:
    if value:  # never cache a failure
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = (time.monotonic(), value)
    return value
# Skip non-illustrative or noisy file types.
_BAD_EXT = re.compile(r"\.(svg|gif|ogg|ogv|webm|pdf|tif|tiff)$", re.IGNORECASE)
# Labelled diagrams in another language ("Plant cell structure-ru-v1.png") are
# useless to our students; Commons marks them in the file name.
_FOREIGN_LABELS = re.compile(r"[-_ (](ru|de|fr|es|it|pt|zh|ja|uk|pl|nl|cs|hu|ar|fa|tr|vi|ko|sr|bg|ca)([-_ ).]|v\d)", re.IGNORECASE)
_BAD_HINT = re.compile(r"(logo|icon|map of|flag of|coat of arms|signature|stub)", re.IGNORECASE)


def _clean_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


async def search_images(query: str, *, limit: int = 6, width: int = 900) -> list[dict[str, Any]]:
    """Search Commons for photos/diagrams matching ``query``.

    Returns [{url, title, description, pageUrl, license}], best-effort (never raises).
    """
    key = ("commons", query.lower(), limit, width)
    if (hit := _cached(key)) is not None:
        return hit
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": f"{query} filetype:bitmap",
        "gsrnamespace": "6",  # File namespace
        "gsrlimit": str(limit * 2),
        "prop": "imageinfo",
        "iiprop": "url|extmetadata|mime",
        "iiurlwidth": str(width),
    }
    try:
        async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(COMMONS_API, params=params)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Wikimedia search failed for %r: %s", query, exc)
        return []

    pages = (data.get("query", {}) or {}).get("pages", {}) or {}
    results: list[dict[str, Any]] = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        title = page.get("title", "").replace("File:", "")
        if _BAD_EXT.search(title) or _BAD_HINT.search(title) or _FOREIGN_LABELS.search(title):
            continue
        mime = info.get("mime", "")
        if mime and not mime.startswith("image/"):
            continue
        url = info.get("thumburl") or info.get("url")
        if not url:
            continue
        meta = info.get("extmetadata", {}) or {}
        desc = _clean_html(meta.get("ImageDescription", {}).get("value", ""))[:400]
        lic = meta.get("LicenseShortName", {}).get("value", "")
        results.append(
            {
                "url": url,
                "title": title,
                "description": desc,
                "pageUrl": info.get("descriptionurl", ""),
                "license": lic,
            }
        )
    return _store(key, results[:limit])


WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"


async def search_wikipedia_images(query: str, *, limit: int = 4, width: int = 800) -> list[dict[str, Any]]:
    """Lead images of the Wikipedia articles matching ``query``.

    Far more relevant than Commons full-text search for teaching: an article's
    lead image is chosen by editors to illustrate exactly that subject
    ("Amoeba" -> a micrograph of an amoeba). Best-effort (never raises).
    """
    key = ("wikipedia", query.lower(), limit, width)
    if (hit := _cached(key)) is not None:
        return hit
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": query, "gsrlimit": str(limit), "gsrnamespace": "0",
        "prop": "pageimages|description|info", "piprop": "thumbnail|name",
        "pithumbsize": str(width), "inprop": "url",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(WIKIPEDIA_API, params=params)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Wikipedia image search failed for %r: %s", query, exc)
        return []
    pages = sorted(((data.get("query", {}) or {}).get("pages", {}) or {}).values(),
                   key=lambda p: p.get("index", 99))
    results: list[dict[str, Any]] = []
    for page in pages:
        thumb = (page.get("thumbnail") or {}).get("source")
        name = page.get("pageimage", "")
        # SVG diagrams are fine here: the thumbnail is a rendered PNG.
        if not thumb or _BAD_HINT.search(name):
            continue
        results.append({
            "url": thumb,
            "title": page.get("title", ""),
            "description": f"{page.get('title', '')} — {page.get('description', '')}".strip(" —"),
            "pageUrl": page.get("fullurl", ""),
            "license": "Wikipedia",
        })
    return _store(key, results)
