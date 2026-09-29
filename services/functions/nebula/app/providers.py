"""Where searches come from. Web: Brave Search (default) or a self-hosted SearXNG,
chosen in Nebula's policy. Video: YouTube's official Data API.

API keys live in .env (BRAVE_API_KEY, YOUTUBE_API_KEY), never in config or code.
These calls go to known, fixed services, so they don't go through the guard.
"""
import html
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from .web import WebError

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
YOUTUBE_URL = "https://www.googleapis.com/youtube/v3"


def _plain(text: str | None) -> str:
    """Search snippets arrive with <strong> tags and HTML entities."""
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _site(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.")


async def brave(client: httpx.AsyncClient, key: str | None, query: str, count: int,
                safesearch: str = "moderate", base: str = BRAVE_URL) -> list[dict[str, Any]]:
    if not key:
        raise WebError("Web search isn't set up yet: add BRAVE_API_KEY to .env on the server.")
    r = await client.get(base, params={"q": query, "count": count, "safesearch": safesearch},
                         headers={"X-Subscription-Token": key, "Accept": "application/json"})
    if r.status_code in (401, 403):
        raise WebError("Brave refused the API key. Check BRAVE_API_KEY in .env.")
    if r.status_code == 429:
        raise WebError("Brave's search limit was reached. Try again in a bit, or check the plan's credit.")
    r.raise_for_status()
    results = (r.json().get("web") or {}).get("results") or []
    return [{"title": _plain(x.get("title")), "url": x.get("url"), "site": _site(x.get("url", "")),
             "snippet": _plain(x.get("description")), "age": x.get("age")} for x in results[:count] if x.get("url")]


async def searxng(client: httpx.AsyncClient, base: str | None, query: str, count: int) -> list[dict[str, Any]]:
    if not base:
        raise WebError("SearXNG is chosen but no address is set (search.searxng_url in Nebula's policy).")
    r = await client.get(base.rstrip("/") + "/search", params={"q": query, "format": "json"})
    r.raise_for_status()
    results = r.json().get("results") or []
    return [{"title": _plain(x.get("title")), "url": x.get("url"), "site": _site(x.get("url", "")),
             "snippet": _plain(x.get("content")), "age": None} for x in results[:count] if x.get("url")]


def duration(iso: str | None) -> str | None:
    """PT1H4M13S → 1:04:13; PT4M3S → 4:03."""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m or not iso:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    h += d * 24
    return f"{h}:{mi:02d}:{s:02d}" if h else f"{mi}:{s:02d}"


def views(n: str | int | None) -> str | None:
    if n is None:
        return None
    n = int(n)
    for div, unit in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + unit + " views"
    return f"{n} views"


async def youtube(client: httpx.AsyncClient, key: str | None, query: str, count: int,
                  safesearch: str = "moderate", base: str = YOUTUBE_URL) -> list[dict[str, Any]]:
    """Two calls: search (100 quota units) then durations and views (1 unit)."""
    if not key:
        raise WebError("YouTube search isn't set up yet: add YOUTUBE_API_KEY to .env on the server.")
    r = await client.get(f"{base}/search", params={"part": "snippet", "type": "video", "maxResults": count,
                                                    "q": query, "safeSearch": safesearch, "key": key})
    if r.status_code == 403:
        reason = ((r.json().get("error") or {}).get("errors") or [{}])[0].get("reason", "")
        raise WebError("YouTube's daily search allowance is used up; it resets at midnight Pacific."
                       if "quota" in reason.lower() else "YouTube refused the API key. Check YOUTUBE_API_KEY in .env.")
    r.raise_for_status()
    items = [i for i in r.json().get("items", []) if (i.get("id") or {}).get("videoId")]
    ids = [i["id"]["videoId"] for i in items]
    details: dict[str, dict] = {}
    if ids:
        d = await client.get(f"{base}/videos", params={"part": "contentDetails,statistics", "id": ",".join(ids), "key": key})
        if d.is_success:
            details = {v["id"]: v for v in d.json().get("items", [])}
    out = []
    for i in items:
        vid, sn = i["id"]["videoId"], i.get("snippet") or {}
        det = details.get(vid, {})
        out.append({"title": _plain(sn.get("title")), "channel": _plain(sn.get("channelTitle")),
                    "video_id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
                    "published": (sn.get("publishedAt") or "")[:10] or None,
                    "duration": duration((det.get("contentDetails") or {}).get("duration")),
                    "views": views((det.get("statistics") or {}).get("viewCount")),
                    "thumbnail": ((sn.get("thumbnails") or {}).get("medium") or {}).get("url")})
    return out
