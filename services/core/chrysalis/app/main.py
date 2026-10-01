"""Chrysalis service: the watch loop, a small status API, and the `chrysalis`
function (status / releases / rollback / approve) so Aurelia can report on and
request update actions — under governance like any other function."""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .docker import Docker
from .git import Git
from .pipeline import Chrysalis
from .report import Reporter
from .settings import Settings
from .store import Store
from .verify import Verifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("chrysalis")
settings = Settings()

REGISTRATION = {
    "name": "chrysalis",
    "description": "Aurelia's update system: deploys new versions from GitHub, tests them, and rolls back failures.",
    "url": settings.function_url,
    "tools": [
        {"name": "status", "description": "Current version, whether an update is in progress, and dry-run mode.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "releases", "description": "Recent releases and how each deploy went (live, rolled back, held). "
                                            "For what a release does, why, and what it lets you do, use "
                                            "Mnemosyne's history.",
         "input_schema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}}}},
        {"name": "rollback", "description": "Roll Aurelia back to the previous good release.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "approve", "description": "Approve a release that is being held for review.",
         "input_schema": {"type": "object", "properties": {"release_id": {"type": "integer"}},
                          "required": ["release_id"]}},
    ],
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
    app.state.chrysalis = Chrysalis(settings, Git(settings.repo_path), Docker(settings.repo_path, settings.project),
                                    Verifier(settings.orchestrator_url), Store(pool), Reporter(settings.orchestrator_url))
    try:
        await app.state.chrysalis.ensure_baseline()
    except Exception:
        log.exception("could not adopt a baseline yet; will keep watching")
    tasks = [asyncio.create_task(app.state.chrysalis.loop()), asyncio.create_task(heartbeat())]
    yield
    for t in tasks:
        t.cancel()
    await pool.close()


app = FastAPI(title="Aurelia core: Chrysalis", lifespan=lifespan)


async def heartbeat() -> None:
    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            try:
                await client.post(f"{settings.orchestrator_url}/registry/functions", json=REGISTRATION)
            except httpx.HTTPError as exc:
                log.warning("registration failed, will retry: %s", exc)
            await asyncio.sleep(settings.heartbeat_seconds)


# ---- status API ------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/status")
async def status():
    c: Chrysalis = app.state.chrysalis
    cfg = c.config()
    live = await c.store.live(1)
    return {
        "version": (await c.git.head())[:8],
        "live_release": _brief(live[0]) if live else None,
        "dry_run": cfg.dry_run, "enabled": cfg.enabled, "branch": cfg.branch,
        "docker": c.state["docker"], "busy": c.state["busy"],
        "last_check": c.state["last_check"], "last_error": c.state["last_error"],
    }


@app.get("/releases")
async def releases(limit: int = 10):
    return [_brief(r) for r in await app.state.chrysalis.store.recent(min(limit, 50))]


@app.post("/check")
async def check_now():
    """Look for new commits right away instead of waiting for the next poll."""
    c: Chrysalis = app.state.chrysalis
    return {"result": await c.cycle(c.config())}


def _brief(r: dict[str, Any]) -> dict[str, Any]:
    import json
    notes = r.get("notes")
    notes = json.loads(notes) if isinstance(notes, str) else notes
    return {"id": r["id"], "sha": r["sha"][:8], "message": r.get("message"), "author": r.get("author"),
            "services": r.get("services"), "status": r["status"], "detail": r.get("detail"),
            "started_at": str(r.get("started_at")), "finished_at": str(r.get("finished_at")),
            **({"purpose": notes.get("purpose"), "summary": notes.get("summary")} if isinstance(notes, dict) else {})}


# ---- function contract (same shape as every Aurelia function) ----------------------

class Invoke(BaseModel):
    task_id: str
    args: dict


@app.post("/tools/{tool}/invoke", status_code=202)
async def invoke(tool: str, body: Invoke):
    if tool not in {"status", "releases", "rollback", "approve"}:
        raise HTTPException(404, f"chrysalis has no tool '{tool}'")
    asyncio.create_task(_run_tool(tool, body.task_id, body.args))
    return {"accepted": body.task_id}


@app.post("/cancel/{task_id}", status_code=202)
async def cancel(task_id: str):
    return {"cancelling": task_id, "note": "Chrysalis actions finish or roll back on their own"}


async def _run_tool(tool: str, task_id: str, args: dict) -> None:
    c: Chrysalis = app.state.chrysalis
    base = f"{settings.orchestrator_url}/tasks/{task_id}"
    try:
        if tool == "status":
            data = await status()
        elif tool == "releases":
            data = {"releases": await releases(int(args.get("limit", 5)))}
        elif tool == "rollback":
            data = {"message": await c.rollback()}
        else:
            data = {"message": await c.approve(int(args["release_id"]))}
        payload = {"status": "completed", "data": data}
    except Exception as exc:
        log.exception("tool %s failed", tool)
        payload = {"status": "failed", "error": str(exc)}
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(f"{base}/result", json=payload)
