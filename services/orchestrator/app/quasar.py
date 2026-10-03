"""Quasar: video and music on his computers (Phase 7, brought forward from Phase 15).

Aurelia doesn't operate players built for eyes (web pages, TV menus). She asks services with
APIs for what to play and hands it to a player she controls: her own player on each computer
(mpv, in the node agent), which tells her exactly what it's doing. Step 1 is his Jellyfin
library; step 2 is his Roku TVs (below); YouTube and Spotify come next.

On a Roku TV (no computer of his by that name): a title from his library opens in the Roku's
Jellyfin app on that exact item, and Jellyfin's own list of what each of its apps is playing
confirms it. Anything else ("play The Bear on the TV", "Stranger Things on Netflix") goes through
Roku's own search, which opens it in whichever of his apps has it (Orbit's tv_play); the TV then
says what it really shows: playing, open in an app, or only found.

  find   his library: search, or (no query) what he can continue, next up and recently added
  play   a movie, a show (next up, or a season and episode), an album, an artist or a song,
         on one of his computers, full screen; confirmed from the player itself

Jellyfin's address and his sign-in stay on the server (.env). A computer gets a short-lived link
per stream (/nodes/media/<ticket>, only with its own key, only for that computer), and Aurelia
streams it through from Jellyfin, so no Jellyfin credential ever reaches a computer. Where he left
off and what he's watched are reported back to Jellyfin as he watches, like any Jellyfin app.
Control while it plays (pause, skip, subtitles, next episode) is Constellation's media.
"""
import asyncio
import json
import logging
import os
import re
import secrets
import time
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import db, nodes, registry

log = logging.getLogger("aurelia.quasar")
router = APIRouter(prefix="/quasar", tags=["quasar"])
node_router = APIRouter(tags=["quasar"])                # the computers' side, under /nodes/ (node keys)
NAME = "quasar"
PLAYS_SINCE = (0, 5, 9)
TICKET = "aurelia:media:ticket:{}"
AUTH = "aurelia:jellyfin:auth"
STATE = "aurelia:player:{}"                             # what a computer's player last said (30 s)
REPORTED = "aurelia:player:{}:jf"                       # what Jellyfin last heard from it
TYPES = {"movies": ["Movie"], "shows": ["Series", "Episode"], "music": ["MusicAlbum", "MusicArtist", "Audio"],
         "any": ["Movie", "Series", "Episode", "MusicAlbum", "MusicArtist", "Audio", "Playlist"]}
FIELDS = "Overview,ProductionYear,RunTimeTicks,UserData,SeriesName,ParentIndexNumber,IndexNumber,AlbumArtist,Album,Artists"
TICKS = 10_000_000
ROKU_JELLYFIN = "Jellyfin"                              # the Jellyfin app on a Roku
ROKU_CONFIRM = 25.0                                     # how long Jellyfin gets to report the Roku playing it
SERVICES = (r"netflix|hulu|(?:amazon )?prime(?: video)?|amazon|disney(?:\+| plus)?|hbo max|max|peacock|"
            r"paramount(?:\+| plus)?|apple tv(?:\+| plus)?|youtube(?: tv)?|tubi|pluto(?: tv)?|crunchyroll|plex|"
            r"jellyfin|starz|showtime|fubo|sling|espn(?:\+| plus)?|the roku channel|roku channel")

TOOLS = [
    registry.ToolSpec(
        name="find", description="His own library (Jellyfin): movies, shows, episodes, music. query: what to look "
                                 "for; kind: movies, shows, music or any. With no query: his whole library as "
                                 "posters (what he can continue, next up and recently added first, then every show, "
                                 "movie and album A to Z); kind with no query: just those (\"show my movies\"). Each "
                                 "result has an id for play.",
        input_schema={"type": "object", "properties": {
            "query": {"type": "string"}, "kind": {"type": "string", "enum": ["movies", "shows", "music", "any"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 40}}},
        cancellable=False, freshness_seconds=60, display="panel"),
    registry.ToolSpec(
        name="play", description="Play something from his library on one of his computers, full screen with my "
                                 "player: what (his words: a movie, a show, an album, an artist, a song) or id (from "
                                 "find); on: which computer or TV ('the TV', 'the desk'; leave out for the one "
                                 "running your shell). A show plays its next-up episode (or season / episode he "
                                 "names) and keeps going; music plays the album or artist in order (shuffle: true "
                                 "to mix). It picks up where he left off unless from_start. The result is checked "
                                 "with the player itself. Control while it plays is Constellation's media. On a "
                                 "Roku TV (no computer by that name), or with app (a streaming service: 'Netflix'), "
                                 "it opens the title on the TV: from his library in the Roku's Jellyfin app, anything "
                                 "else through Roku's search in whichever of his apps has it. Say what the result "
                                 "says the TV really shows (playing, open in an app, or only found).",
        input_schema={"type": "object", "properties": {
            "what": {"type": "string"}, "id": {"type": "string"}, "on": {"type": "string"},
            "app": {"type": "string", "description": "a streaming service, only if he named one"},
            "season": {"type": "integer"}, "episode": {"type": "integer"},
            "shuffle": {"type": "boolean"}, "from_start": {"type": "boolean"}}},
        cancellable=False),
]


# ---- Jellyfin ---------------------------------------------------------------------------------------

class NotSetUp(ValueError):
    pass


class Jellyfin:
    """His Jellyfin, signed in as him (so resume points and watched marks are his)."""

    WAKING = (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError, httpx.ReadError)

    def __init__(self, redis, url: str | None = None, user: str | None = None, password: str | None = None,
                 wake_seconds: float | None = None):
        self.redis = redis
        self.wake_seconds = float(wake_seconds if wake_seconds is not None else os.environ.get("JELLYFIN_WAKE_SECONDS", 60))
        self.retry_every = 2.0
        self.url = (url if url is not None else os.environ.get("JELLYFIN_URL", "")).rstrip("/")
        self.user = user if user is not None else os.environ.get("JELLYFIN_USER", "")
        self.password = password if password is not None else os.environ.get("JELLYFIN_PASSWORD", "")
        if not (self.url and self.user):
            raise NotSetUp("Jellyfin isn't set up yet: add JELLYFIN_URL, JELLYFIN_USER and JELLYFIN_PASSWORD to the "
                           "server's .env (its address on the home-lab network, e.g. http://lazytainer-b:8096).")

    async def call(self, client: httpx.AsyncClient, method: str, url: str, stream: bool = False, **kw) -> httpx.Response:
        """One request, retried while Jellyfin wakes: it may sleep behind Lazytainer, which starts it when
        traffic arrives and refuses or drops connections meanwhile."""
        deadline = time.monotonic() + self.wake_seconds
        while True:
            try:
                req = client.build_request(method, url, **kw)
                r = await client.send(req, stream=stream)
                if r.status_code in (502, 503, 504) and time.monotonic() < deadline:
                    await r.aclose()
                    await asyncio.sleep(self.retry_every)
                    continue
                return r
            except self.WAKING as exc:
                if time.monotonic() >= deadline:
                    raise ValueError(f"I can't reach Jellyfin at {self.url} ({exc.__class__.__name__}); is "
                                     "JELLYFIN_URL right, and is the orchestrator on the home-lab network?")
                await asyncio.sleep(self.retry_every)

    async def ping(self) -> dict:
        """Jellyfin's public server info (no sign-in needed): proves the address works."""
        async with httpx.AsyncClient(timeout=15) as c:
            r = await self.call(c, "GET", f"{self.url}/System/Info/Public")
        r.raise_for_status()
        d = r.json()
        return {"server": d.get("ServerName"), "version": d.get("Version")}

    @staticmethod
    def header(token: str | None = None) -> dict[str, str]:
        h = 'MediaBrowser Client="Aurelia", Device="Aurelia", DeviceId="aurelia-orchestrator", Version="1.0"'
        return {"Authorization": h + (f', Token="{token}"' if token else "")}

    async def auth(self, fresh: bool = False) -> dict:
        if not fresh:
            raw = await self.redis.get(AUTH)
            if raw:
                return json.loads(raw)
        async with httpx.AsyncClient(timeout=15) as c:
            try:
                r = await self.call(c, "POST", f"{self.url}/Users/AuthenticateByName", headers=self.header(),
                                    json={"Username": self.user, "Pw": self.password})
            except httpx.HTTPError as exc:
                raise ValueError(f"I can't reach Jellyfin at {self.url} ({exc.__class__.__name__}).")
        if r.status_code in (401, 403):
            raise ValueError("Jellyfin didn't accept the sign-in in .env (JELLYFIN_USER / JELLYFIN_PASSWORD).")
        r.raise_for_status()
        d = r.json()
        got = {"token": d["AccessToken"], "user_id": d["User"]["Id"]}
        await self.redis.set(AUTH, json.dumps(got))          # never sent anywhere; Jellyfin can revoke it
        return got

    async def request(self, method: str, path: str, params: dict | None = None, body: Any = None) -> Any:
        for attempt in (0, 1):
            a = await self.auth(fresh=attempt == 1)
            q = {k: v for k, v in (params or {}).items() if v is not None}
            q = {k: (a["user_id"] if v == "{user}" else v) for k, v in q.items()}
            async with httpx.AsyncClient(timeout=20) as c:
                try:
                    r = await self.call(c, method, f"{self.url}{path.replace('{user}', a['user_id'])}", params=q,
                                        json=body, headers=self.header(a["token"]))
                except httpx.HTTPError as exc:
                    raise ValueError(f"I can't reach Jellyfin ({exc.__class__.__name__}).")
            if r.status_code == 401 and attempt == 0:
                continue
            if r.status_code >= 400:
                raise ValueError(f"Jellyfin said no ({r.status_code}).")
            return r.json() if r.content and "json" in r.headers.get("content-type", "") else None
        return None

    async def items(self, **params) -> list[dict]:
        return (await self.page(**params))[0]

    async def page(self, **params) -> tuple[list[dict], int]:
        """Items and how many there are in all (for "showing 500 of 812")."""
        base = {"userId": "{user}", "recursive": "true", "fields": FIELDS, "enableUserData": "true"}
        d = await self.request("GET", "/Items", {**base, **params}) or {}
        got = d.get("Items") or []
        return got, int(d.get("TotalRecordCount") or len(got))

    async def item(self, item_id: str) -> dict:
        got = await self.items(ids=item_id)
        if not got:
            raise ValueError("That isn't in your library any more.")
        return got[0]

    async def search(self, query: str, kind: str = "any", limit: int = 20) -> list[dict]:
        return await self.items(searchTerm=query, includeItemTypes=",".join(TYPES.get(kind, TYPES["any"])), limit=limit)

    async def next_up(self, series_id: str | None = None, limit: int = 12) -> list[dict]:
        d = await self.request("GET", "/Shows/NextUp", {"userId": "{user}", "seriesId": series_id, "limit": limit,
                                                        "fields": FIELDS, "enableUserData": "true"})
        return (d or {}).get("Items") or []

    async def episodes(self, series_id: str, season: int | None = None) -> list[dict]:
        d = await self.request("GET", f"/Shows/{series_id}/Episodes", {"userId": "{user}", "season": season,
                                                                       "fields": FIELDS, "enableUserData": "true"})
        eps = (d or {}).get("Items") or []
        return sorted(eps, key=lambda e: (e.get("ParentIndexNumber") or 0, e.get("IndexNumber") or 0))

    async def sessions(self) -> list[dict]:
        """Every Jellyfin app in use right now, with what it's playing (how a Roku is checked)."""
        return await self.request("GET", "/Sessions", {"activeWithinSeconds": 120}) or []

    async def report(self, what: str, body: dict) -> None:
        path = {"start": "/Sessions/Playing", "progress": "/Sessions/Playing/Progress",
                "stop": "/Sessions/Playing/Stopped"}[what]
        await self.request("POST", path, body=body)

    async def stream(self, item_id: str, kind: str, rng: str | None):
        a = await self.auth()
        path = f"/{'Audio' if kind == 'audio' else 'Videos'}/{item_id}/stream"
        client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=None))
        headers = self.header(a["token"]) | ({"Range": rng} if rng else {})
        r = await self.call(client, "GET", f"{self.url}{path}", stream=True, params={"static": "true"}, headers=headers)
        if r.status_code == 401:
            await r.aclose()
            a = await self.auth(fresh=True)
            req = client.build_request("GET", f"{self.url}{path}", params={"static": "true"},
                                       headers=self.header(a["token"]) | ({"Range": rng} if rng else {}))
            r = await client.send(req, stream=True)
        return client, r


def shape(it: dict) -> dict:
    """One library item, the way the panel and the model see it."""
    t = it.get("Type") or ""
    ud = it.get("UserData") or {}
    out = {"id": it.get("Id"), "type": {"Movie": "movie", "Series": "show", "Episode": "episode", "Season": "season",
                                        "MusicAlbum": "album", "MusicArtist": "artist", "Audio": "song",
                                        "Playlist": "playlist"}.get(t, t.lower()),
           "name": it.get("Name") or "", "art": f"/quasar/art/{it.get('Id')}"}
    if it.get("ProductionYear"):
        out["year"] = it["ProductionYear"]
    if it.get("RunTimeTicks"):
        out["minutes"] = round(it["RunTimeTicks"] / TICKS / 60)
    if t == "Episode":
        out.update(show=it.get("SeriesName") or "", season=it.get("ParentIndexNumber"), episode=it.get("IndexNumber"))
        if it.get("SeriesId"):
            out["art"] = f"/quasar/art/{it['SeriesId']}"
    if t in ("Audio", "MusicAlbum"):
        out["artist"] = it.get("AlbumArtist") or ", ".join(it.get("Artists") or [])
        if t == "Audio" and it.get("Album"):
            out["album"] = it["Album"]
    if ud.get("PlaybackPositionTicks"):
        out["resume_minutes"] = round(ud["PlaybackPositionTicks"] / TICKS / 60)
    if ud.get("Played"):
        out["watched"] = True
    if it.get("Overview"):
        out["about"] = " ".join(str(it["Overview"]).split())[:280]
    return out


def brief(it: dict) -> dict:
    """An item in a long list: no description (a whole library stays light)."""
    out = shape(it)
    out.pop("about", None)
    return out


def label(it: dict) -> str:
    if it.get("Type") == "Episode":
        se = f"S{it.get('ParentIndexNumber') or 0}E{it.get('IndexNumber') or 0}"
        return f"{it.get('SeriesName') or ''} · {se} · {it.get('Name') or ''}".strip(" ·")
    if it.get("Type") == "Audio":
        who = it.get("AlbumArtist") or ", ".join(it.get("Artists") or [])
        return f"{who} — {it.get('Name')}" if who else it.get("Name") or ""
    year = f" ({it['ProductionYear']})" if it.get("ProductionYear") else ""
    return f"{it.get('Name') or ''}{year}"


def _norm(t: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (t or "").lower()))


def best(hits: list[dict], asked: str) -> dict | None:
    """Exact name first (a show over its episodes), else Jellyfin's own order."""
    if not hits:
        return None
    a = _norm(asked)
    rank = {"Series": 0, "Movie": 0, "MusicArtist": 1, "MusicAlbum": 1, "Playlist": 1, "Episode": 2, "Audio": 2}
    exact = [h for h in hits if _norm(h.get("Name")) == a]
    pool = exact or [h for h in hits if a and a in _norm(h.get("Name"))] or hits
    return sorted(pool, key=lambda h: rank.get(h.get("Type"), 3))[0]


# ---- the tools -------------------------------------------------------------------------------------

async def find(app, user: str, args: dict) -> dict:
    jf = Jellyfin(app.state.redis)
    q = " ".join(str(args.get("query") or "").split())
    kind = args.get("kind") if args.get("kind") in TYPES else "any"
    limit = int(args.get("limit") or 20)
    if q:
        got = [shape(i) for i in await jf.search(q, kind, limit)]
        head = f"{len(got)} in your library for “{q}”" if got else f"Nothing in your library for “{q}”"
        return {"headline": head, "catalog": {"query": q, "items": got}}
    cap = app.state.config.current.quasar.library_limit
    groups = []
    if kind == "any":                                      # the whole library: what's going on first
        resume = await jf.items(filters="IsResumable", sortBy="DatePlayed", sortOrder="Descending",
                                includeItemTypes="Movie,Episode", limit=8)
        up = await jf.next_up(limit=8)
        recent = await jf.items(sortBy="DateCreated", sortOrder="Descending", includeItemTypes="Movie,Series", limit=8)
        groups = [{"name": "Continue", "items": [shape(i) for i in resume]},
                  {"name": "Next up", "items": [shape(i) for i in up if i.get("Id") not in {r.get("Id") for r in resume}]},
                  {"name": "Recently added", "items": [shape(i) for i in recent]}]
    total = 0
    for name, types, k in (("Shows", "Series", "shows"), ("Movies", "Movie", "movies"), ("Music", "MusicAlbum", "music")):
        if kind not in ("any", k):
            continue
        got, n = await jf.page(includeItemTypes=types, sortBy="SortName", sortOrder="Ascending", limit=cap)
        total += n
        groups.append({"name": name, "items": [brief(i) for i in got], "total": n})
    groups = [g for g in groups if g["items"]]
    what = {"any": "Your library", "shows": "Your shows", "movies": "Your movies", "music": "Your music"}[kind]
    head = f"{what} · {total} title{'s' if total != 1 else ''}" if groups else f"{what}: nothing yet"
    return {"headline": head, "catalog": {"groups": groups}}


async def _queue(jf: Jellyfin, it: dict, args: dict, app) -> list[dict]:
    """What to play, in order, for what he picked."""
    t = it.get("Type")
    cfg = app.state.config.current.quasar
    if t == "Series":
        eps = await jf.episodes(it["Id"])
        if not eps:
            raise ValueError(f"{it.get('Name')} has no episodes in your library.")
        s, e = args.get("season"), args.get("episode")
        if s or e:
            hits = [x for x in eps if (not s or x.get("ParentIndexNumber") == int(s)) and (not e or x.get("IndexNumber") == int(e))]
            if not hits:
                raise ValueError(f"{it.get('Name')} has no " + (f"season {s} " if s else "") + (f"episode {e}" if e else "") + ".")
            start = eps.index(hits[0])
        else:
            up = await jf.next_up(it["Id"], 1)
            ids = [x.get("Id") for x in eps]
            start = ids.index(up[0]["Id"]) if up and up[0].get("Id") in ids else 0
        return eps[start:start + 1 + cfg.queue_episodes]
    if t == "Season":
        return await jf.items(parentId=it["Id"], includeItemTypes="Episode", sortBy="IndexNumber")
    if t == "Episode" and it.get("SeriesId"):
        eps = await jf.episodes(it["SeriesId"])
        ids = [x.get("Id") for x in eps]
        start = ids.index(it["Id"]) if it["Id"] in ids else 0
        return eps[start:start + 1 + cfg.queue_episodes] if eps else [it]
    if t == "MusicAlbum":
        return await jf.items(parentId=it["Id"], includeItemTypes="Audio",
                              sortBy="Random" if args.get("shuffle") else "ParentIndexNumber,IndexNumber")
    if t == "MusicArtist":
        return await jf.items(artistIds=it["Id"], includeItemTypes="Audio", limit=100,
                              sortBy="Random" if args.get("shuffle") else "Album,ParentIndexNumber,IndexNumber")
    if t == "Playlist":
        return await jf.items(parentId=it["Id"], limit=200, **({"sortBy": "Random"} if args.get("shuffle") else {}))
    return [it]


def _version(v) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(v or ""))[:3]) or (0,)


async def _where(app, asked: str) -> dict:
    from . import constellation
    try:
        return await constellation.target(app, asked or None, screen=True)
    except constellation.NoSuchNode as exc:
        linked = await constellation.node_on_tv(app, asked) if asked else None
        if linked:                                          # "on the living room TV": the computer plugged into it
            return linked
        raise ValueError(str(exc))


def split_service(on: str) -> tuple[str | None, str]:
    """'netflix' or 'netflix on the living room tv' → ('netflix', 'living room tv'); else (None, on)."""
    m = re.fullmatch(rf"(?:the )?({SERVICES})(?:\s+on\s+(?:the |my )?(.+))?", on.strip().lower())
    if not m:
        return None, on
    return m.group(1), (on.strip()[m.start(2):] if m.group(2) else "")


class NoRoku(ValueError):
    pass


async def _orbit(app, user: str, tool: str, args: dict, wait: float = 45) -> dict:
    """An Orbit TV tool, as a task like any other (same governance and log). NoRoku when there's no
    Roku TV by that name (or no Orbit at all)."""
    from fastapi import HTTPException
    from . import tasks
    try:
        tid = await tasks.launch(app, tasks.StartTask(tool=f"orbit.{tool}", args=args, user=user, trigger="quasar",
                                                       display="brief"))
    except HTTPException as exc:
        d = exc.detail if isinstance(exc.detail, dict) else {}
        if d.get("needs_confirmation"):                     # its governance entry is missing (or says ask him)
            raise ValueError(f"orbit.{tool} isn't set to run on request (governance has it as '{d.get('tier')}', "
                             "so it would need your confirmation, and I can't ask from inside Quasar). Its entry "
                             "is in config/governance/functions/orbit.yaml.")
        if exc.status_code in (403, 429):
            raise ValueError(str(d.get("reason") or exc.detail))
        raise NoRoku(str(exc.detail))
    res = await tasks.wait_for_result(tid, wait)
    if res is None:
        raise ValueError("The TV hasn't answered yet, so I can't say it's playing.")
    if res["status"] != "completed":
        err = str(res.get("error") or "")
        low = err.lower()
        if "no tv matches" in low or "there's no tv" in low or "couldn't find a roku" in low or "no device" in low:
            raise NoRoku(err)
        raise ValueError(err or "The TV didn't do it.")
    data = res.get("result") or {}
    return json.loads(data) if isinstance(data, str) else data


async def _jellyfin_on_roku(app, user: str, jf: "Jellyfin", tv: str, queue: list[dict]) -> dict | None:
    """The Roku's Jellyfin app, opened on exactly this item; confirmed only when Jellyfin itself lists
    that app playing it. None when the Roku has no Jellyfin app (then Roku's search is tried)."""
    first = queue[0]
    kind = {"Movie": "movie", "Episode": "episode", "Audio": "audio"}.get(first.get("Type"), "movie")
    try:
        opened = await _orbit(app, user, "tv_app", {"name": tv, "app": ROKU_JELLYFIN, "content_id": first["Id"],
                                                    "media_type": kind})
    except NoRoku:
        raise
    except ValueError as exc:
        if "no app called" in str(exc).lower():
            return None
        raise
    tv_name = opened.get("name") or tv
    ids = {q.get("Id") for q in queue}
    loop = asyncio.get_running_loop()
    end = loop.time() + ROKU_CONFIRM
    while loop.time() < end:
        for se in await jf.sessions():
            now = se.get("NowPlayingItem") or {}
            if now.get("Id") in ids:
                return {"headline": f"Playing {label(now)} on {tv_name}",
                        "did": {"node": tv_name, "what": f"Playing {label(now)} in Jellyfin (Jellyfin confirms it)"}}
        await asyncio.sleep(1.0)
    return {"headline": f"Asked {tv_name} to play {label(first)}",
            "did": {"node": tv_name, "what": f"Jellyfin is open on {tv_name}, but it hasn't started {label(first)} "
                                             "(Jellyfin doesn't list it playing). Press play there, or ask me again.",
                    "unconfirmed": True}}


async def _search_on_roku(app, user: str, tv: str, title: str, service: str | None, kind: str | None,
                          season: int | None) -> dict:
    """Roku's own search opens it in whichever of his apps has it; the TV says what it shows."""
    args = {"name": tv, "title": title, **({"app": service} if service else {}), **({"kind": kind} if kind else {}),
            **({"season": season} if season else {})}
    d = await _orbit(app, user, "tv_play", args)
    tv_name = d.get("name") or tv
    if d.get("confirmed"):
        return {"headline": f"Playing {title} on {tv_name}", "did": {"node": tv_name, "what": d.get("message")}}
    head = f"{title} is open in {d['in']} on {tv_name}" if d.get("play") == "opened" else f"Found {title} on {tv_name}"
    return {"headline": head, "did": {"node": tv_name, "what": d.get("message"), "unconfirmed": True}}


async def on_roku(app, user: str, tv: str, what: str, args: dict, service: str | None = None) -> dict:
    """A title on a Roku TV: from his library through its Jellyfin app, else Roku's search."""
    jf = Jellyfin(app.state.redis)
    kind = {"Movie": "movie", "Series": "show", "Episode": "show"}
    season = int(args["season"]) if str(args.get("season") or "").isdigit() else None
    it = None
    if service in (None, "jellyfin"):
        try:
            it = await jf.item(str(args["id"])) if args.get("id") else best(await jf.search(what, "any", 25), what)
        except NotSetUp:
            pass
        if it is not None and it.get("Type") not in ("MusicAlbum", "MusicArtist", "Audio", "Playlist"):
            queue = [q for q in await _queue(jf, it, args, app) if q.get("Id")]
            if queue:
                done = await _jellyfin_on_roku(app, user, jf, tv, queue)
                if done is not None:
                    return done
            what = it.get("Name") or what
        if service == "jellyfin":
            raise ValueError(f"I couldn't find “{what}” in your library.")
    t = "show" if season else kind.get((it or {}).get("Type") or "")      # a library title says which it is
    return await _search_on_roku(app, user, tv, what, service, t, season)


async def play(app, user: str, args: dict) -> dict:
    from . import constellation
    jf = Jellyfin(app.state.redis)
    on = " ".join(str(args.get("on") or "").split())
    what_said = " ".join(str(args.get("what") or "").split())
    service, tv_said = split_service(on)
    if service is None and args.get("app"):
        service, tv_said = split_service(str(args["app"]))[0], on
    if service:                                             # "on Netflix (on the TV)": a streaming app, on a Roku
        if not what_said and not args.get("id"):
            raise ValueError("Play what?")
        try:
            return await on_roku(app, user, tv_said, what_said, args, service)
        except NoRoku as exc:
            raise ValueError(f"{service.title()} plays on a Roku TV, and I can't find "
                             + (f"one called “{tv_said}”." if tv_said else "one.") + f" ({exc})")
    try:
        node = await _where(app, on)
    except ValueError as exc:
        if not on:
            raise
        if args.get("id"):                                  # from his library, on a Roku TV of that name?
            try:
                return await on_roku(app, user, on, what_said, args)
            except NoRoku:
                raise exc
        node = None                                         # "Murder on the Orient Express": "on" was part of the title
    if args.get("id"):
        it = await jf.item(str(args["id"]))
    else:
        what = " ".join(str(args.get("what") or "").split())
        if not what:
            raise ValueError("Play what? (a movie, a show, an album, an artist or a song)")
        if node is None:                                    # no computer by that name: was it the title?
            whole = f"{what} on {on}"
            it = best(await jf.search(whole, "any", 25), whole)
            if it is None:                                  # no: a Roku TV of that name, then
                try:
                    return await on_roku(app, user, on, what, args)
                except NoRoku:
                    await _where(app, on)                   # say which computer wasn't found
            node = await _where(app, "")
        else:
            it = best(await jf.search(what, "any", 25), what)
        if it is None and on:                               # not in his library: the TV's apps may have it
            try:
                return await on_roku(app, user, on, what, args)
            except NoRoku:
                pass
        if it is None:
            raise ValueError(f"I couldn't find “{what}” in your library.")
    queue = [q for q in await _queue(jf, it, args, app) if q.get("Id")]
    if not queue:
        raise ValueError(f"There's nothing to play in {it.get('Name')}.")
    info = await db.pool().fetchval("SELECT info FROM devices WHERE id=$1", node["id"])
    info = json.loads(info) if isinstance(info, str) else (info or {})
    if _version(info.get("agent_version")) < PLAYS_SINCE:
        raise ValueError(f"{node['name']} is running an older agent without my player; it updates itself within ten minutes.")
    cfg = app.state.config.current.quasar
    redis = app.state.redis
    raw = await redis.get(STATE.format(node["id"]))
    before = json.loads(raw) if raw else {}
    items = []
    for q in queue[:200]:
        kind = "audio" if q.get("Type") == "Audio" else "video"
        t = secrets.token_hex(16)
        await redis.set(TICKET.format(t), json.dumps({"node": node["id"], "item": q["Id"], "kind": kind}),
                        ex=int(cfg.ticket_hours * 3600))
        items.append({"path": f"/nodes/media/{t}", "title": label(q), "item": f"jf:{q['Id']}", "kind": kind})
    first = queue[0]
    start = 0.0
    if cfg.resume and not args.get("from_start"):
        start = ((first.get("UserData") or {}).get("PlaybackPositionTicks") or 0) / TICKS
    tv = asyncio.create_task(constellation.show_on_tv(app, user, node))   # the TV wakes and switches meanwhile
    try:
        data = await constellation._ask_new(app, node, "play", {"items": items, "start": start}, timeout=40)
    except ValueError as exc:
        await tv
        if "isn't installed" in str(exc) and "mpv" in str(exc):     # offer to put my player there
            return {"headline": f"{node['name']} needs my player first",
                    "did": {"node": node["name"], "what": "My player (mpv) isn't installed there"},
                    "confirm": {"tool": "constellation.install_package",
                                "reason": f"{node['name']} needs my player (mpv) to play video and music. Install it "
                                          "from Ubuntu's packages?",
                                "reply": "Installed my player. Ask me to play it again.",
                                "args": {"target": node["name"], "name": "mpv", "source": "apt"}}}
        raise
    except BaseException:
        await tv
        raise
    st = data.get("player") or {}
    if not st.get("active"):
        raise ValueError(f"{node['name']}'s player didn't start it.")
    title = st.get("title") or label(first)
    switched = before.get("active") and before.get("title") and before["title"] != title
    out = {"headline": f"Switched to {title} on {node['name']}" if switched else f"Playing {title} on {node['name']}",
           "now": now_block(node["name"], st, {q["Id"]: q for q in queue})}
    if start > 5:
        out["now"]["resumed"] = True
    return {**out, **await tv}


def now_block(node_name: str, st: dict, known: dict | None = None) -> dict:
    """What a player is doing, for the panel (the same shape whoever asks)."""
    item = str(st.get("item") or "")
    jid = item[3:] if item.startswith("jf:") else None
    meta = (known or {}).get(jid) if jid else None
    out = {"node": node_name, "active": bool(st.get("active")), "title": st.get("title") or "",
           "kind": st.get("kind") or "video", "position": st.get("position"), "duration": st.get("duration"),
           "paused": bool(st.get("paused")), "volume": st.get("volume"), "muted": bool(st.get("muted")),
           "subtitles": st.get("subtitles"), "next": st.get("next"), "index": st.get("index"), "count": st.get("count")}
    if jid:
        out["art"] = f"/quasar/art/{(meta or {}).get('SeriesId') or (meta or {}).get('AlbumId') or jid}"
    return {k: v for k, v in out.items() if v is not None}


HANDLERS = {"find": find, "play": play}


# ---- the computers' side: streams and what their players say ------------------------------------------

@node_router.get("/nodes/media/{ticket}", include_in_schema=False)
async def media_stream(ticket: str, request: Request):
    """A stream for one computer's player, through from Jellyfin (seeking works: ranges pass through)."""
    dev = await nodes.node_from(request)
    if not re.fullmatch(r"[0-9a-f]{32}", ticket):
        raise HTTPException(404)
    raw = await request.app.state.redis.get(TICKET.format(ticket))
    if not raw:
        raise HTTPException(404, "That link has expired.")
    t = json.loads(raw)
    if t["node"] != dev["id"]:                               # one computer's link is useless to another
        raise HTTPException(404)
    try:
        client, r = await Jellyfin(request.app.state.redis).stream(t["item"], t["kind"], request.headers.get("range"))
    except (NotSetUp, ValueError, httpx.HTTPError) as exc:
        raise HTTPException(502, f"Jellyfin: {exc}")
    if r.status_code >= 400:
        await r.aclose()
        await client.aclose()
        raise HTTPException(502, f"Jellyfin said {r.status_code}")
    keep = {k: v for k, v in r.headers.items()
            if k.lower() in ("content-type", "content-length", "content-range", "accept-ranges", "last-modified", "etag")}

    async def close():
        await r.aclose()
        await client.aclose()
    return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=keep, background=BackgroundTask(close))


class PlayerState(BaseModel):
    active: bool = False
    title: str = ""
    item: str | None = None
    kind: str = "video"
    position: float | None = None
    duration: float | None = None
    paused: bool = False
    volume: float | None = None
    muted: bool = False
    index: int | None = None
    count: int | None = None
    subtitles: str | None = None
    next: str | None = None
    last: dict | None = None


@node_router.post("/nodes/player/state", include_in_schema=False)
async def player_state(body: PlayerState, request: Request):
    """A computer's player, every few seconds while it plays and once when it stops. Kept briefly
    for "what's playing", and passed on to Jellyfin so his resume points and watched marks are right."""
    dev = await nodes.node_from(request)
    redis = request.app.state.redis
    st = body.model_dump(exclude_none=True)
    await redis.set(STATE.format(dev["id"]), json.dumps({**st, "at": time.time()}), ex=30)
    try:
        await _tell_jellyfin(redis, dev["id"], st)
    except (NotSetUp, ValueError) as exc:
        log.info("couldn't report playback to Jellyfin: %s", exc)
    return {"ok": True}


def _ticks(sec) -> int:
    return int(max(0.0, float(sec or 0)) * TICKS)


async def _tell_jellyfin(redis, node_id: int, st: dict) -> None:
    key = REPORTED.format(node_id)
    raw = await redis.get(key)
    prev = json.loads(raw) if raw else None
    item = str(st.get("item") or "")
    jid = item[3:] if st.get("active") and item.startswith("jf:") else None
    jf = None

    async def j():
        nonlocal jf
        jf = jf or Jellyfin(redis)
        return jf
    if prev and prev.get("id") != jid:                        # it moved on, or stopped: where it ended
        last = st.get("last") or {}
        pos = last.get("position") if str(last.get("item") or "") == f"jf:{prev['id']}" else prev.get("position")
        await (await j()).report("stop", {"ItemId": prev["id"], "PositionTicks": _ticks(pos)})
        await redis.delete(key)
    if jid:
        body = {"ItemId": jid, "PositionTicks": _ticks(st.get("position")), "IsPaused": bool(st.get("paused")),
                "CanSeek": True, "PlayMethod": "DirectPlay", "VolumeLevel": int(st.get("volume") or 0),
                "IsMuted": bool(st.get("muted"))}
        await (await j()).report("progress" if prev and prev.get("id") == jid else "start", body)
        await redis.set(key, json.dumps({"id": jid, "position": st.get("position")}), ex=6 * 3600)


@router.get("/art/{item_id}", include_in_schema=False)
async def art(item_id: str, request: Request):
    """A poster or album cover from Jellyfin, for the panels (kept by the browser for a day)."""
    if not re.fullmatch(r"[0-9A-Za-z-]{1,64}", item_id):
        raise HTTPException(404)
    jf = Jellyfin(request.app.state.redis)
    a = await jf.auth()
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            r = await jf.call(c, "GET", f"{jf.url}/Items/{item_id}/Images/Primary", params={"maxWidth": 400, "quality": 80},
                              headers=jf.header(a["token"]))
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502)
    if r.status_code != 200:
        raise HTTPException(404)
    return Response(r.content, media_type=r.headers.get("content-type", "image/jpeg"),
                    headers={"Cache-Control": "private, max-age=86400"})


# ---- registration (like any function) ---------------------------------------------------------------

async def register_loop(app) -> None:
    from .settings import settings
    while True:
        ttl = app.state.config.current.registry.function_ttl_seconds
        try:
            await registry.register(app.state.redis, registry.FunctionRegistration(
                name=NAME, description="Video and music on his computers: his Jellyfin library, played full screen "
                                       "by Aurelia's own player.",
                url=f"{settings.self_url.rstrip('/')}/quasar", tools=TOOLS), ttl)
        except Exception:
            log.exception("couldn't register Quasar; will retry")
        await asyncio.sleep(max(3, ttl // 3))


@router.get("/health")
async def health(request: Request):
    try:
        Jellyfin(request.app.state.redis)
    except NotSetUp:
        return {"status": "ok", "jellyfin": "not set up"}
    return {"status": "ok", "jellyfin": "set"}


@router.get("/check")
async def check(request: Request):
    """Can I reach Jellyfin and sign in? (For setting up: wakes it if it's asleep, up to a minute.)"""
    try:
        jf = Jellyfin(request.app.state.redis)
        info = await jf.ping()
        await jf.auth(fresh=True)
    except (NotSetUp, ValueError, httpx.HTTPError) as exc:
        return {"ok": False, "problem": str(exc)}
    return {"ok": True, **info, "signed_in_as": jf.user}


class Invoke(BaseModel):
    task_id: str
    args: dict = {}


@router.post("/tools/{tool}/invoke", status_code=202)
async def invoke(tool: str, body: Invoke, request: Request):
    if tool not in HANDLERS:
        raise HTTPException(404, f"quasar has no tool '{tool}'")
    asyncio.create_task(_run(request.app, tool, body.task_id, body.args))
    return {"accepted": body.task_id}


@router.post("/cancel/{task_id}", status_code=202)
async def cancel(task_id: str):
    return {"cancelling": task_id}


async def _run(app, tool: str, task_id: str, args: dict) -> None:
    from . import tasks
    try:
        row = await db.pool().fetchrow("SELECT user_id FROM task_runs WHERE id=$1", uuid.UUID(task_id))
        data = await HANDLERS[tool](app, row["user_id"] if row else "owner", args)
        result = tasks.Result(status="completed", data=data)
    except ValueError as exc:
        result = tasks.Result(status="failed", error=str(exc))
    except Exception as exc:
        log.exception("quasar.%s failed", tool)
        result = tasks.Result(status="failed", error=f"Something went wrong: {exc}")
    await tasks._finish(tasks._AppContext(app), task_id, f"{NAME}.{tool}", result)
