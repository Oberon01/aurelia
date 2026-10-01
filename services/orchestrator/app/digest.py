"""The morning digest: one panel on your first interaction after the digest hour
(the display opening, or the first thing you ask). Parallax writes it; quiet days
produce none. Shown at most once a day."""
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Request

from . import db, registry
from .tasks import StartTask, launch

log = logging.getLogger("aurelia.digest")
router = APIRouter(prefix="/digest", tags=["digest"])


async def maybe_show(app, user: str = "owner", device: int | None = None) -> dict:
    cfg = app.state.config.current.digest
    if not cfg.enabled:
        return {"shown": False, "why": "turned off"}
    now = datetime.now(ZoneInfo(cfg.timezone))
    if now.hour < cfg.hour:
        return {"shown": False, "why": "too early"}
    day = now.date()
    if await db.pool().fetchval("SELECT 1 FROM digests WHERE day=$1", day):
        return {"shown": False, "why": "already shown today"}
    redis = app.state.redis
    lock = f"aurelia:digest:{day}"
    if not await redis.set(lock, "1", nx=True, ex=300):            # two interactions at once: one digest
        return {"shown": False, "why": "in progress"}
    try:
        resolved = await registry.resolve(redis, "parallax.digest")
        if resolved is None:
            await redis.delete(lock)
            return {"shown": False, "why": "Parallax isn't running"}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{resolved.url}/digest")
            r.raise_for_status()
            content = r.json()
        await db.pool().execute("INSERT INTO digests (day, content) VALUES ($1, $2::jsonb) ON CONFLICT DO NOTHING",
                                day, json.dumps(content))
        if content.get("quiet"):
            return {"shown": False, "why": "quiet day"}
        task_id = await launch(app, StartTask(tool="parallax.digest", args={}, trigger="digest", user=user,
                                              device=device))
        return {"shown": True, "task_id": task_id}
    except Exception as exc:
        log.warning("digest not shown: %s", exc)
        await redis.delete(lock)
        return {"shown": False, "why": str(exc)}


@router.post("/check")
async def check(request: Request, user: str = "owner"):
    """The display calls this when it opens; /ask calls it on your first request. It opens on
    that display. A node's screen (a TV running her shell) doesn't bring it up on its own."""
    from . import auth
    dev = await auth.asker(request.headers)
    if dev and dev["kind"] == "screen":
        return {"shown": False, "why": "not on a node's screen unless asked"}
    return await maybe_show(request.app, user, await auth.display_of(request.headers))
