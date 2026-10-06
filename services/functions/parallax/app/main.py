"""Parallax — Aurelia's analytics function (Phase 4).

Reads her task history (read-only), works out what it means (recurring habits,
what matters most, what's used enough to keep longer, timing drift) and proposes
changes to the review mailbox. Proposals only take effect when Kentrell approves
them on the display; Parallax has no way to approve anything.

Tools:
- analyze  run the analysis now (it also runs on its own every hour)
- mailbox  show what's waiting for review
- digest   the morning digest (the orchestrator shows it once a day)
"""
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pydantic_settings import BaseSettings

from . import analysis
from .analysis import Rules

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("function.parallax")


class Settings(BaseSettings):
    orchestrator_url: str = "http://orchestrator:8000"
    function_url: str = "http://parallax:9000"
    database_url: str
    heartbeat_seconds: int = 10
    user: str = "owner"


settings = Settings()

TOOLS_SPEC = [
    {"name": "analyze", "description": "Look through Aurelia's task history now for habits worth automating, "
                                       "what matters most, and timing changes; new ideas go to the mailbox.",
     "input_schema": {"type": "object", "properties": {}}, "progress_unit": "steps", "refreshes": ["parallax.mailbox"]},
    {"name": "mailbox", "description": "Show the review mailbox: Aurelia's proposals waiting for Kentrell's decision. "
                                       "Only he can decide, on the display.",
     "input_schema": {"type": "object", "properties": {}}, "freshness_seconds": 10},
    {"name": "digest", "description": "The morning digest: what's waiting, what ran automatically, failures, "
                                      "anything unusual.",
     "input_schema": {"type": "object", "properties": {}}, "freshness_seconds": 300},
]
REGISTRATION = {"name": "parallax",
                "description": "Analytics: learns from Aurelia's task history (habits, what matters most, timing) "
                               "and proposes changes for your approval.",
                "url": settings.function_url, "tools": [{**t, "cancellable": True} for t in TOOLS_SPEC]}

POLICY: dict = {}
_cancelled: set[str] = set()


def rules() -> Rules:
    return Rules.from_policy((POLICY.get("policy") or {}).get("rules"))


def interval_minutes() -> float:
    return float(((POLICY.get("policy") or {}).get("rules") or {}).get("analyze_minutes", 60))


# ---- reading history (read-only) ------------------------------------------------------------

async def history(days: int = 35) -> list[dict]:
    rows = await app.state.db.fetch(
        "SELECT tool, args, status, trigger, tier, started_at, duration_ms, error FROM task_runs "
        "WHERE user_id = $1 AND started_at > now() - make_interval(days => $2) ORDER BY started_at",
        settings.user, days)
    return [{**dict(r), "args": json.loads(r["args"]) if isinstance(r["args"], str) else (r["args"] or {})}
            for r in rows]


async def current_settings() -> tuple[dict[str, str], dict[str, int]]:
    levels = {r["tool"]: r["level"] for r in await app.state.db.fetch("SELECT tool, level FROM tool_criticality")}
    kept = {r["tool"]: r["hot_days"] for r in await app.state.db.fetch("SELECT tool, hot_days FROM tool_retention")}
    return levels, kept


async def orchestrator(method: str, path: str, **kw) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.request(method, f"{settings.orchestrator_url}{path}", **kw)
        r.raise_for_status()
        return r.json()


# ---- the analysis ---------------------------------------------------------------------------

async def run_analysis(progress: Callable[[int, str], Awaitable[None]] | None = None) -> dict:
    async def step(i: int, label: str) -> None:
        if progress:
            await progress(i, label)

    now = datetime.now(timezone.utc)
    r = rules()
    await step(0, "Reading history")
    rows = await history()
    levels, kept = await current_settings()
    await step(1, "Looking for habits")
    ideas = analysis.recurrences(rows, now, r)
    await step(2, "Weighing what matters most")
    crit = analysis.criticality(rows, now, r, levels)
    ideas += crit
    critical = {t for t, lvl in levels.items() if lvl == "critical"} | {
        p["proposal"]["tool"] for p in crit if p["proposal"]["level"] == "critical"}
    await step(3, "Checking what's used most")
    ideas += analysis.retention(rows, now, r, kept, critical)
    insights = analysis.drift(rows, now, r)
    await step(4, "Sending ideas to the mailbox")
    results = []
    for idea in ideas:
        try:
            res = await orchestrator("POST", "/mailbox/proposals", json={**idea, "source": "parallax"})
            results.append({"title": idea["title"], "kind": idea["kind"], "result": res.get("result")})
        except httpx.HTTPError as exc:
            log.warning("couldn't propose %s: %s", idea["fingerprint"], exc)
    days = len({x["started_at"].date() for x in rows})
    return {"history": f"{len(rows)} tasks over {days} day{'s' if days != 1 else ''}",
            "ideas": results, "insights": [i["text"] for i in insights],
            "message": _summary(results, days)}


def _summary(results: list[dict], days: int) -> str:
    new = sum(1 for x in results if x["result"] == "created")
    if new:
        return f"{new} new idea{'s' if new != 1 else ''} in the mailbox."
    if not results:
        return ("Nothing to suggest yet. Habits need at least a week or two of history to show."
                if days < 7 else "Nothing new to suggest.")
    return "Nothing new; the mailbox is up to date."


async def build_digest() -> dict:
    now = datetime.now(timezone.utc)
    r = rules()
    rows = await history()
    waiting = (await orchestrator("GET", "/mailbox"))["items"]
    releases = [dict(x) for x in await app.state.db.fetch(
        "SELECT sha, message, status FROM releases WHERE started_at > now() - interval '24 hours' "
        "AND status IN ('live', 'rolled_back', 'failed', 'held') ORDER BY id DESC")]
    computers = []
    try:                                   # Constellation keeps each computer's latest health problems
        for c in await app.state.db.fetch(
                "SELECT name, info->'issues' AS issues FROM devices WHERE kind='node' AND status='approved' "
                "AND jsonb_array_length(COALESCE(info->'issues', '[]'::jsonb)) > 0 "
                "AND last_seen > now() - interval '1 day' ORDER BY name"):
            computers.append({"name": c["name"], "issues": json.loads(c["issues"]) if isinstance(c["issues"], str) else c["issues"]})
    except Exception as exc:               # an older database without devices: no section
        log.info("no computer health for the digest: %s", exc)
    notices = []
    try:                                   # what Pulsar noticed: open now, or raised in the last day
        rank = {"critical": 0, "high": 1, "normal": 2, "low": 3}
        notices = sorted([dict(x) for x in await app.state.db.fetch(
            "SELECT title, severity, status FROM notices WHERE status='open' OR first_at > now() - interval '24 hours' "
            "ORDER BY first_at DESC LIMIT 20")], key=lambda n: (n["status"] != "open", rank.get(n["severity"], 9)))
    except Exception as exc:               # an older database without notices: no section
        log.info("no notices for the digest: %s", exc)
    calendar, email = [], []
    try:                                   # Beacon: today on your calendars, and important email since yesterday
        tz = ZoneInfo(r.timezone)
        start = datetime.combine(now.astimezone(tz).date(), datetime.min.time(), tz)
        calendar = [dict(x) for x in await app.state.db.fetch(
            "SELECT title, starts_at, ends_at, all_day, location, source FROM calendar_events WHERE starts_at < $2 "
            "AND COALESCE(ends_at, starts_at) > $1 ORDER BY all_day DESC, starts_at LIMIT 30", start, start + timedelta(days=1))]
        email = [dict(x) for x in await app.state.db.fetch(
            "SELECT from_name, from_addr, subject, why FROM mail_items WHERE important AND received_at > now() - "
            "interval '24 hours' ORDER BY received_at DESC LIMIT 20")]
    except Exception as exc:               # an older database without Beacon's tables: no sections
        log.info("no calendar or email for the digest: %s", exc)
    return analysis.digest(rows, now, r, waiting, releases, analysis.drift(rows, now, r), computers, notices,
                           calendar, email)


async def analysis_loop() -> None:
    await asyncio.sleep(60)                           # let the rest of Aurelia come up first
    while True:
        try:
            out = await run_analysis()
            log.info("analysis: %s; %s", out["history"], out["message"])
        except Exception:
            log.exception("analysis failed; will try again")
        await asyncio.sleep(max(5.0, interval_minutes()) * 60)


# ---- lifecycle ------------------------------------------------------------------------------

async def heartbeat() -> None:
    global POLICY
    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            try:
                await client.post(f"{settings.orchestrator_url}/registry/functions", json=REGISTRATION)
                r = await client.get(f"{settings.orchestrator_url}/governance/functions/parallax")
                if r.is_success:
                    POLICY = r.json()
            except httpx.HTTPError as exc:
                log.warning("registration failed, will retry: %s", exc)
            await asyncio.sleep(settings.heartbeat_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=3)
    tasks = [asyncio.create_task(heartbeat()), asyncio.create_task(analysis_loop())]
    yield
    for t in tasks:
        t.cancel()
    await app.state.db.close()


app = FastAPI(title="Aurelia function: Parallax", lifespan=lifespan)


# ---- tools ----------------------------------------------------------------------------------

class TaskContext:
    def __init__(self, task_id: str, args: dict, client: httpx.AsyncClient):
        self.task_id, self.args, self._client = task_id, args, client
        self._base = f"{settings.orchestrator_url}/tasks/{task_id}"

    @property
    def cancelled(self) -> bool:
        return self.task_id in _cancelled

    async def progress(self, current: float, total: float | None, unit: str, label: str | None = None) -> None:
        try:
            r = await self._client.post(f"{self._base}/progress", json={
                "current": current, "total": total, "unit": unit, "label": label})
            if r.is_success and r.json().get("cancel"):
                _cancelled.add(self.task_id)
        except httpx.HTTPError:
            pass


async def analyze(ctx: TaskContext) -> dict:
    return await run_analysis(lambda i, label: ctx.progress(i, 5, "steps", label))


async def mailbox(ctx: TaskContext) -> dict:
    out = await orchestrator("GET", "/mailbox")
    return {"mailbox": out["items"], "counts": out["counts"]}


async def digest(ctx: TaskContext) -> dict:
    return {"digest": await build_digest()}


TOOLS: dict[str, Callable[[TaskContext], Awaitable[dict]]] = {"analyze": analyze, "mailbox": mailbox, "digest": digest}


class Invoke(BaseModel):
    task_id: str
    args: dict


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/digest")
async def digest_preview():
    """For the orchestrator: today's digest, to decide whether to show it (quiet days don't)."""
    return await build_digest()


@app.post("/tools/{tool}/invoke", status_code=202)
async def invoke(tool: str, body: Invoke):
    if tool not in TOOLS:
        raise HTTPException(404, f"parallax has no tool '{tool}'")
    asyncio.create_task(run(TOOLS[tool], body.task_id, body.args))
    return {"accepted": body.task_id}


@app.post("/cancel/{task_id}", status_code=202)
async def cancel(task_id: str):
    _cancelled.add(task_id)
    return {"cancelling": task_id}


async def run(tool: Callable[[TaskContext], Awaitable[dict]], task_id: str, args: dict) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        ctx = TaskContext(task_id, args, client)
        try:
            payload = {"status": "completed", "data": await tool(ctx)}
        except Exception as exc:
            log.exception("task %s failed", task_id)
            payload = {"status": "failed", "error": f"Parallax couldn't finish: {exc}"}
        finally:
            _cancelled.discard(task_id)
        try:
            await client.post(f"{settings.orchestrator_url}/tasks/{task_id}/result", json=payload)
        except httpx.HTTPError:
            log.error("couldn't report result for %s", task_id)
