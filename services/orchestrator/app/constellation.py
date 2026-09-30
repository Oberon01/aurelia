"""Constellation: his devices and the machines Aurelia runs on (Phase 7).

Part of the orchestrator (like Mnemosyne), because devices authenticate here and
nodes will hold their connection here. Step 1 has one tool: the devices panel,
listing everything paired with Aurelia. Pairing and revoking happen on the
display (or the server), never through a tool: Aurelia can show the list but
can't let a device in or lock one out.
"""
import asyncio
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import auth, db, registry

log = logging.getLogger("aurelia.constellation")
router = APIRouter(prefix="/constellation", tags=["constellation"])
NAME = "constellation"

TOOLS = [
    registry.ToolSpec(
        name="devices", description="Every device paired with Aurelia (browsers, the phone, computers running "
                                    "her node, terminals): what it is, when it was last seen, and any waiting to pair. "
                                    "Revoking or approving happens on the display, not through you.",
        input_schema={"type": "object", "properties": {}}, cancellable=False, freshness_seconds=30, display="panel"),
]


async def devices(app, user: str, args: dict) -> dict:
    items = await auth.listing()
    live = [d for d in items if d["status"] == "approved"]
    waiting = [d for d in items if d["status"] == "pending"]
    head = f"{len(live)} paired" + (f" · {len(waiting)} waiting" if waiting else "")
    return {"headline": head, "paired": items}


HANDLERS = {"devices": devices}


async def register_loop(app) -> None:
    from .settings import settings
    while True:
        ttl = app.state.config.current.registry.function_ttl_seconds
        try:
            await registry.register(app.state.redis, registry.FunctionRegistration(
                name=NAME, description="His devices: what's paired with Aurelia, and (soon) the computers she runs on.",
                url=f"{settings.self_url.rstrip('/')}/constellation", tools=TOOLS), ttl)
        except Exception:
            log.exception("couldn't register Constellation; will retry")
        await asyncio.sleep(max(3, ttl // 3))


class Invoke(BaseModel):
    task_id: str
    args: dict = {}


@router.post("/tools/{tool}/invoke", status_code=202)
async def invoke(tool: str, body: Invoke, request: Request):
    if tool not in HANDLERS:
        raise HTTPException(404, f"constellation has no tool '{tool}'")
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
    except Exception as exc:
        log.exception("constellation.%s failed", tool)
        result = tasks.Result(status="failed", error=f"Something went wrong: {exc}")
    await tasks._finish(tasks._AppContext(app), task_id, f"{NAME}.{tool}", result)
