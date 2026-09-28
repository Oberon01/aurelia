"""Probe — Aurelia's diagnostics function, and the reference for every function.

A *function* is one containerized service grouping related tools under a
space-themed name. Every function follows the same contract:

1. Register itself and all its tools with the orchestrator at startup, and
   heartbeat, so a new version replaces the old one with no orchestrator restart.
2. Accept POST /tools/<tool>/invoke and return at once; work in the background.
3. Report progress in real units to /tasks/{id}/progress.
4. Honour cancellation (POST /cancel/{id}, or the flag returned with each
   progress report): stop cleanly and report a partial result.

Probe's one tool, `echo`, counts through `steps` one second at a time and then
repeats `message`, so you can watch progress, cancellation and results end to end.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pydantic_settings import BaseSettings

log = logging.getLogger("function.probe")
logging.basicConfig(level=logging.INFO)


class Settings(BaseSettings):
    orchestrator_url: str = "http://orchestrator:8000"
    function_url: str = "http://probe:9000"
    heartbeat_seconds: int = 10


settings = Settings()

REGISTRATION = {
    "name": "probe",
    "description": "Diagnostics: tools for testing and inspecting Aurelia herself.",
    "url": settings.function_url,
    "tools": [
        {
            "name": "echo",
            "description": "Counts through a number of steps, then repeats a message back. For testing the task loop.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "steps": {"type": "integer", "minimum": 0, "maximum": 600, "default": 5},
                },
                "required": ["message"],
            },
            "progress_unit": "steps",
            "freshness_seconds": None,
            "cancellable": True,
        },
    ],
}

# Local cancellation flags. Set by /cancel (fast path) or by the cancel flag the
# orchestrator returns on each progress report (reaches us even mid-swap).
_cancelled: set[str] = set()

# This function's rules, fetched from the orchestrator on every heartbeat so
# policy edits reach it within seconds. Function-specific rules are enforced here.
POLICY: dict = {}


async def heartbeat() -> None:
    global POLICY
    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            try:
                await client.post(f"{settings.orchestrator_url}/registry/functions", json=REGISTRATION)
                r = await client.get(f"{settings.orchestrator_url}/governance/functions/probe")
                if r.is_success:
                    POLICY = r.json()
            except httpx.HTTPError as exc:
                log.warning("registration failed, will retry: %s", exc)
            await asyncio.sleep(settings.heartbeat_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    beat = asyncio.create_task(heartbeat())
    yield
    beat.cancel()


app = FastAPI(title="Aurelia function: Probe", lifespan=lifespan)


class Invoke(BaseModel):
    task_id: str
    args: dict


class TaskContext:
    """What a tool gets: its args, progress reporting, and a cancel check."""

    def __init__(self, task_id: str, args: dict, client: httpx.AsyncClient):
        self.task_id, self.args, self._client = task_id, args, client
        self._base = f"{settings.orchestrator_url}/tasks/{task_id}"

    @property
    def cancelled(self) -> bool:
        return self.task_id in _cancelled

    async def progress(self, current: float, total: float | None, unit: str, label: str | None = None) -> None:
        r = await self._client.post(f"{self._base}/progress", json={
            "current": current, "total": total, "unit": unit, "label": label})
        if r.is_success and r.json().get("cancel"):
            _cancelled.add(self.task_id)

    async def result(self, data: dict, status: str = "completed", partial: bool = False) -> None:
        await self._client.post(f"{self._base}/result", json={"status": status, "partial": partial, "data": data})


# ---- tools ---------------------------------------------------------------

async def echo(ctx: TaskContext) -> None:
    message = str(ctx.args.get("message", ""))
    steps = max(0, min(int(ctx.args.get("steps", 5)), 600))
    for i in range(1, steps + 1):
        if ctx.cancelled:
            await ctx.result({"message": message, "completed_steps": i - 1, "total_steps": steps},
                             status="cancelled", partial=True)
            return
        await asyncio.sleep(1)
        await ctx.progress(i, steps, "steps", f"Step {i} of {steps}")
    await ctx.result({"message": message, "completed_steps": steps})


TOOLS: dict[str, Callable[[TaskContext], Awaitable[None]]] = {"echo": echo}


# ---- contract endpoints (same in every function) --------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/tools/{tool}/invoke", status_code=202)
async def invoke(tool: str, body: Invoke):
    if tool not in TOOLS:
        raise HTTPException(404, f"probe has no tool '{tool}'")
    asyncio.create_task(run(TOOLS[tool], body.task_id, body.args))
    return {"accepted": body.task_id}


@app.post("/cancel/{task_id}", status_code=202)
async def cancel(task_id: str):
    _cancelled.add(task_id)
    return {"cancelling": task_id}


async def run(tool: Callable[[TaskContext], Awaitable[None]], task_id: str, args: dict) -> None:
    async with httpx.AsyncClient(timeout=5) as client:
        ctx = TaskContext(task_id, args, client)
        try:
            await tool(ctx)
        except Exception as exc:  # report failure rather than dying silently
            log.exception("task %s failed", task_id)
            try:
                await client.post(f"{settings.orchestrator_url}/tasks/{task_id}/result",
                                  json={"status": "failed", "error": str(exc)})
            except httpx.HTTPError:
                pass
        finally:
            _cancelled.discard(task_id)
