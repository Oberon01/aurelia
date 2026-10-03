"""Task lifecycle: start → progress → result/cancel.

Nothing here lives in process memory. The task row is in Postgres, events go
through Redis, and tools call back over HTTP to whichever replica answers —
so an orchestrator can be swapped mid-task without the task noticing.
"""
import json
import logging
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import auth, db, mailbox, memory, registry
from .events import PanelEvent, publish

log = logging.getLogger("aurelia.tasks")
router = APIRouter(prefix="/tasks", tags=["tasks"])
CANCEL_PREFIX = "aurelia:cancel:"


class StartTask(BaseModel):
    tool: str
    args: dict[str, Any] = {}
    trigger: str = "user"
    user: str = "owner"
    confirmed: bool = False        # set by the display after you approve a "change"-tier action
    display: str = "panel"         # "brief": the answer is in her reply, so no panel opens
    device: int | None = None      # the display that asked (set from the proxy's header, not by clients)


class Progress(BaseModel):
    current: float
    total: float | None = None     # None = indeterminate
    unit: str | None = None
    label: str | None = None


class Result(BaseModel):
    status: str = "completed"      # completed | cancelled | failed
    partial: bool = False
    data: dict[str, Any] = {}
    error: str | None = None


def _redis(request: Request):
    return request.app.state.redis


@router.post("", status_code=202)
async def start_task(body: StartTask, request: Request):
    headers = getattr(request, "headers", None)
    if headers is not None and headers.get("x-aurelia-device"):
        # Asked from a device through the proxy: its panel goes to that display (if it is one).
        body = body.model_copy(update={"device": await auth.display_of(headers)})
    resolved = await registry.resolve(_redis(request), body.tool)
    if resolved is None:
        raise HTTPException(404, f"tool '{body.tool}' is not registered (use '<function>.<tool>')")
    name = resolved.qualified_name

    # Governance: the charter and the function's policy decide, not the function itself.
    gov = request.app.state.governance
    decision = gov.decide(resolved.function, resolved.tool.name, body.user, body.confirmed)
    if decision.allowed and not await _within_rate_limit(request, resolved.function, gov.rate_limit(resolved.function)):
        decision = decision.model_copy(update={"allowed": False, "reason": f"rate limit reached for '{resolved.function}'"})
    if not decision.allowed:
        await db.pool().execute(
            "INSERT INTO governance_denials (user_id, tool, tier, reason, args) VALUES ($1, $2, $3, $4, $5::jsonb)",
            body.user, name, decision.tier, decision.reason, _json(body.args),
        )
        if decision.needs_confirmation:
            raise HTTPException(409, {"needs_confirmation": True, "tier": decision.tier, "reason": decision.reason})
        status = 429 if decision.reason.startswith("rate limit") else 403
        raise HTTPException(status, {"tier": decision.tier, "reason": decision.reason})

    task_id = str(uuid.uuid4())
    await db.pool().execute(
        "INSERT INTO task_runs (id, tool, args, trigger, progress_unit, user_id, tier, confirmed, device) "
        "VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8, $9)",
        uuid.UUID(task_id), name, _json(body.args), body.trigger, resolved.tool.progress_unit,
        body.user, decision.tier, body.confirmed, body.device,
    )
    open_data: dict[str, Any] = {"args": body.args, "user": body.user, "trigger": body.trigger,
                                 "cancellable": resolved.tool.cancellable, "unit": resolved.tool.progress_unit,
                                 "tier": decision.tier,
                                 # Same tool + same key = the same panel: a repeat refreshes it in place.
                                 "key": memory.args_key(body.args),
                                 "refreshes": resolved.tool.refreshes}
    if body.display == "brief":
        open_data["display"] = "brief"
    level = await mailbox.criticality(name)
    if level != "normal":
        open_data["critical"] = level          # its failures stand out on the display
    if decision.tier in ("act", "change"):
        # Actions confirm and get out of the way; look-ups stay until dismissed.
        open_data["close_after"] = request.app.state.config.current.display.action_panel_seconds
    # Memory: a repeated look-up shows its last panel at once (labelled with its
    # age) while this run refreshes it. Actions never restore an old result.
    if (decision.tier in memory.RESTORE_TIERS and request.app.state.config.current.memory.restore
            and not memory.is_system_user(gov, body.user)):
        try:
            snap = await memory.latest(name, body.args, body.user, resolved.tool.freshness_seconds)
            if snap:
                open_data["snapshot"] = snap
        except Exception:
            log.exception("couldn't look up a saved panel for %s", name)
    await publish(_redis(request), PanelEvent(type="panel.open", task_id=task_id, tool=name, data=open_data,
                                              device=body.device))
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{resolved.url}/tools/{resolved.tool.name}/invoke",
                                  json={"task_id": task_id, "args": body.args})
            r.raise_for_status()
    except httpx.HTTPError as exc:
        await _finish(request, task_id, name, Result(status="failed", error=str(exc)))
        raise HTTPException(502, f"function '{resolved.function}' did not accept the task: {exc}")
    return {"task_id": task_id}


@router.post("/{task_id}/progress")
async def report_progress(task_id: str, body: Progress, request: Request):
    """Records progress and tells the tool whether it has been asked to stop.
    The reply carries the cancel flag, so cancellation reaches the tool even if
    the direct /cancel call landed on a replica being swapped out."""
    redis = _redis(request)
    cancel = bool(await redis.exists(CANCEL_PREFIX + task_id))
    row = await db.pool().fetchrow(
        "UPDATE task_runs SET progress_current=$2, progress_total=$3, progress_unit=COALESCE($4, progress_unit) "
        "WHERE id=$1 AND status='running' RETURNING tool, progress_unit, device",
        uuid.UUID(task_id), body.current, body.total, body.unit,
    )
    if row is None:
        return {"cancel": True}  # finished or unknown: tell the tool to stop
    await publish(redis, PanelEvent(
        type="panel.progress", task_id=task_id, tool=row["tool"],
        data={"current": body.current, "total": body.total, "unit": row["progress_unit"], "label": body.label},
        device=row["device"],
    ))
    return {"cancel": cancel}


@router.post("/{task_id}/result", status_code=204)
async def report_result(task_id: str, body: Result, request: Request):
    row = await db.pool().fetchrow("SELECT tool FROM task_runs WHERE id=$1", uuid.UUID(task_id))
    if row is None:
        raise HTTPException(404, "unknown task")
    await _finish(request, task_id, row["tool"], body)


@router.post("/{task_id}/cancel", status_code=202)
async def cancel_task(task_id: str, request: Request):
    row = await db.pool().fetchrow(
        "SELECT tool FROM task_runs WHERE id=$1 AND status='running'", uuid.UUID(task_id))
    if row is None:
        raise HTTPException(404, "no running task with that id")
    resolved = await registry.resolve(_redis(request), row["tool"])
    if resolved is not None and not resolved.tool.cancellable:
        raise HTTPException(409, "tool is not cancellable")
    # Durable flag: returned on the tool's next progress report.
    await _redis(request).set(CANCEL_PREFIX + task_id, 1, ex=3600)
    if resolved is not None:  # fast path: tell the function directly
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(f"{resolved.url}/cancel/{task_id}")
        except httpx.HTTPError:
            pass  # the flag above still reaches it
    # The tool stops cleanly and reports its partial result through /result.
    return {"task_id": task_id, "status": "cancelling"}


@router.get("/summary")
async def task_summary(request: Request, days: int = 7, function: str | None = None):
    """Recent use per tool (people's requests, not system checks): runs, failures, typical time, last run."""
    system = [u for u, e in request.app.state.governance.charter.users.items() if e.role == "system"]
    rows = await db.pool().fetch(
        "SELECT tool, count(*) AS runs, count(*) FILTER (WHERE status='failed') AS failed, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)::bigint AS median_ms, "
        "max(started_at) AS last_run FROM task_runs "
        "WHERE started_at > now() - make_interval(days => $1) AND ($2::text IS NULL OR tool LIKE $2 || '.%') "
        "AND user_id <> ALL($3::text[]) GROUP BY tool ORDER BY tool", min(max(days, 1), 365), function, system)
    return {"days": days, "tools": [{**dict(r), "last_run": r["last_run"].isoformat()} for r in rows]}


@router.get("/{task_id}")
async def get_task(task_id: str):
    row = await db.pool().fetchrow("SELECT * FROM task_runs WHERE id=$1", uuid.UUID(task_id))
    if row is None:
        raise HTTPException(404, "unknown task")
    return dict(row)


# ---- used by the assistant (/ask) -----------------------------------------

class _AppContext:
    """Lets internal callers reuse the route handlers, which only need request.app."""
    def __init__(self, app):
        self.app = app


async def launch(app, body: StartTask) -> str:
    """Start a task through the exact same path as POST /tasks — registry,
    governance, rate limits, logging. Raises HTTPException on refusal."""
    return (await start_task(body, _AppContext(app)))["task_id"]


async def wait_for_result(task_id: str, seconds: float) -> dict[str, Any] | None:
    """Poll until the task finishes or `seconds` pass. None = still running."""
    import asyncio
    import json
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        row = await db.pool().fetchrow(
            "SELECT status, partial, result, error, duration_ms FROM task_runs WHERE id=$1", uuid.UUID(task_id))
        if row and row["status"] != "running":
            out = dict(row)
            out["result"] = json.loads(out["result"]) if out["result"] else None
            return out
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(0.2)


async def _finish(request: Request, task_id: str, tool: str, body: Result) -> None:
    await db.pool().execute(
        "UPDATE task_runs SET status=$2, partial=$3, result=$4::jsonb, error=$5, finished_at=now(), "
        "duration_ms=(EXTRACT(EPOCH FROM now() - started_at) * 1000)::bigint "
        "WHERE id=$1 AND status='running'",
        uuid.UUID(task_id), body.status, body.partial, _json(body.data), body.error,
    )
    redis = _redis(request)
    device = await db.pool().fetchval("SELECT device FROM task_runs WHERE id=$1", uuid.UUID(task_id))
    if body.data and body.status != "failed":
        try:
            row = await db.pool().fetchrow("SELECT args, user_id FROM task_runs WHERE id=$1", uuid.UUID(task_id))
            if row and not memory.is_system_user(request.app.state.governance, row["user_id"]):
                args = row["args"] if isinstance(row["args"], dict) else json.loads(row["args"])
                await memory.save(task_id, tool, args, row["user_id"], body.data, body.partial)
        except Exception:
            log.exception("couldn't save the panel for task %s", task_id)
    if body.data:
        await publish(redis, PanelEvent(type="panel.result", task_id=task_id, tool=tool,
                                        data={"status": body.status, "partial": body.partial, **body.data},
                                        device=device))
    # Nothing to read → the display flashes "Complete" and fades the panel out.
    await publish(redis, PanelEvent(type="panel.close", task_id=task_id, tool=tool,
                                    data={"status": body.status, "has_result": bool(body.data),
                                          "error": body.error}, device=device))
    if device is not None:                  # it took a while and he isn't looking: tell his phone
        import asyncio
        from . import push
        asyncio.create_task(push.task_done(request.app, task_id, tool, body.status, body.data, body.error, device))


async def _within_rate_limit(request: Request, function: str, per_minute: int) -> bool:
    """Fixed one-minute window per function, counted in Redis (shared by all replicas)."""
    import time
    key = f"aurelia:rate:{function}:{int(time.time() // 60)}"
    redis = _redis(request)
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 120)
    return count <= per_minute


def _json(value: Any) -> str:
    import json
    return json.dumps(value)
