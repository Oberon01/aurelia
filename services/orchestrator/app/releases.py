"""Releases on the display: Chrysalis reports each step of a release here, and
the display shows a "New release detected!" panel that fills up to "live".

Chrysalis sends its whole state every time (see services/core/chrysalis/app/report.py),
so a missed update costs nothing. The latest state is also kept in Redis: a
display that reconnects (the orchestrator itself may be the thing being swapped)
asks GET /releases/current and replays the panel from it.
"""
import json
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from .events import PanelEvent, publish

router = APIRouter(prefix="/releases", tags=["releases"])
KEY = "aurelia:release:current"
TOOL = "chrysalis.release"
RUNNING_TTL, FINISHED_TTL = 3600, 120          # a finished release is replayed for 2 minutes
LIVE_FADE_SECONDS = 20

TITLES = {"live": "Release live", "rolled_back": "Release rolled back", "failed": "Release failed",
          "held": "Release waiting for approval", "dry_run": "Release checked (dry run)"}


class ReleaseState(BaseModel):
    release_id: int
    sha: str
    author: str = ""
    message: str = ""
    services: list[str] = []
    current: float = 0
    total: float = 0
    label: str | None = None
    outcome: str | None = None      # live | rolled_back | failed | held | dry_run; None while running
    detail: str | None = None


def events_for(state: ReleaseState, include_open: bool = True) -> list[PanelEvent]:
    """The panel events that show this state."""
    tid = f"release-{state.release_id}"
    out = []
    if include_open:
        out.append(PanelEvent(type="panel.open", task_id=tid, tool=TOOL, data={
            "args": {"sha": state.sha}, "key": state.sha, "tier": "reference", "trigger": "release",
            "cancellable": False, "unit": "steps", "refreshes": ["chrysalis.releases", "probe.functions"],
            "subtitle": state.message or state.sha}))
    if state.outcome is None:
        out.append(PanelEvent(type="panel.progress", task_id=tid, tool=TOOL, data={
            "current": state.current, "total": state.total or None, "unit": "steps", "label": state.label}))
    else:
        out.append(PanelEvent(type="panel.result", task_id=tid, tool=TOOL, data={
            "status": "completed", "partial": False, "title": TITLES.get(state.outcome, "Release"),
            "release": {"sha": state.sha, "message": state.message, "author": state.author,
                        "services": state.services, "outcome": state.outcome, "detail": state.detail}}))
        out.append(PanelEvent(type="panel.close", task_id=tid, tool=TOOL, data={
            "status": "completed", "has_result": True,
            # A release that went live fades on its own; anything else stays until you read it.
            "close_after": LIVE_FADE_SECONDS if state.outcome in ("live", "dry_run") else None}))
    return out


@router.post("/progress", status_code=204)
async def progress(body: ReleaseState, request: Request):
    redis = request.app.state.redis
    raw = await redis.get(KEY)
    seen = raw is not None and json.loads(raw).get("release_id") == body.release_id
    await redis.set(KEY, body.model_dump_json(), ex=RUNNING_TTL if body.outcome is None else FINISHED_TTL)
    for event in events_for(body, include_open=not seen):
        await publish(redis, event)
    return Response(status_code=204)


@router.get("/current")
async def current(request: Request) -> dict[str, Any]:
    """The release in progress (or just finished), as panel events to replay."""
    raw = await request.app.state.redis.get(KEY)
    if raw is None:
        return {"events": []}
    return {"events": [e.model_dump() for e in events_for(ReleaseState.model_validate_json(raw))]}
