"""Beacon's side of the orchestrator: times read with my own parser, your calendar on the day tape, and the second
path for critical notices (your ntfy, through Beacon, which holds its topic).

- /when/resolve: Beacon asks me what "thursday at 3pm", "tomorrow" or "this week" means, so a calendar event and
  a reminder read the same words the same way (when.py), and an unclear time is asked about, never guessed.
- A critical notice also goes to your ntfy (and a high one, if pulsar.yaml's ntfy_for says so): a second path
  for when the app's own push doesn't reach your phone.
"""
import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import registry, when
from . import net

log = logging.getLogger("aurelia.beacon")
router = APIRouter(tags=["beacon"])

MAX_DAYS = 31


def _internal(request: Request) -> None:
    """Only my own functions: from my stack's network, never through the proxy."""
    from . import peers
    host = request.client.host if request.client else None
    if request.headers.get("x-aurelia-device") or request.headers.get("x-forwarded-host") or not peers.from_stack(host):
        raise HTTPException(403, "Only from inside the stack.")


class Resolve(BaseModel):
    text: str
    need: str = "time"              # time: an exact moment · split: "<what> <when>" · day: a day or a few


def days(text: str, now: datetime) -> tuple[date, int]:
    """'today', 'tomorrow', 'thursday', 'oct 8', 'this week', 'next week', 'the weekend' -> (first day, how many)."""
    t = " ".join(str(text or "").lower().replace(",", " ").split()).strip(" .?!")
    t = re.sub(r"^(?:on|for|in)\s+", "", t)
    today = now.date()
    if t in ("", "today", "now", "today's", "todays"):
        return today, 1
    if t in ("this week", "the week", "week", "the next week", "next 7 days", "the next 7 days", "coming week"):
        return today, 7
    if t in ("next week",):
        start = today + timedelta(days=7 - today.weekday())
        return start, 7
    if t in ("the weekend", "this weekend", "weekend"):
        sat = today + timedelta(days=(5 - today.weekday()) % 7)
        return (today, 1) if today.weekday() == 6 else (sat, 2)
    d, _, rest = when._day(t, today)
    if d is None or rest:
        raise when.Unclear(f"Which day is “{text}”? Say “today”, “tomorrow”, “Thursday” or “Oct 8”.")
    return d, 1


@router.post("/when/resolve")
async def resolve(body: Resolve, request: Request):
    _internal(request)
    tz = request.app.state.config.current.digest.timezone
    now = datetime.now(ZoneInfo(tz))
    try:
        if body.need == "day":
            first, n = days(body.text, now)
            return {"day": first.isoformat(), "days": min(n, MAX_DAYS), "tz": tz}
        if body.need == "split":
            at, what, words = when.split(body.text, now, tz)
            return {"at": at.isoformat(), "what": what, "when": words, "tz": tz, "says": when.say(at, now)}
        at = when.parse(body.text, now, tz)
        return {"at": at.isoformat(), "tz": tz, "says": when.say(at, now)}
    except when.Unclear as exc:
        raise HTTPException(422, str(exc))


# ---- the second path for critical notices ------------------------------------------------------------------

def ntfy_for(app) -> set[str]:
    from . import pulsar
    got = pulsar.rules(app).get("ntfy_for")
    return {str(x) for x in (got if isinstance(got, list) else ["critical"])}


async def second_path(app, notice: dict) -> bool:
    """A notice of a severity his rules name also goes to his ntfy, through Beacon (which holds its address and
    topic). True when ntfy took it. Never raises: the first path already went."""
    if notice.get("severity") not in ntfy_for(app):
        return False
    try:
        resolved = await registry.resolve(app.state.redis, "beacon.status")
        if resolved is None:
            return False
        async with net.session(10) as c:
            r = await c.post(f"{resolved.url.rstrip('/')}/notify", json={
                "title": str(notice.get("title") or "")[:120], "text": str(notice.get("text") or "")[:400],
                "severity": notice.get("severity"), "tag": f"notice-{notice.get('id')}"})
            return r.is_success and bool(r.json().get("sent"))
    except Exception as exc:                               # ntfy not set up, Beacon restarting: the push still went
        log.info("second path for notice %s: %s", notice.get("id"), exc)
        return False
