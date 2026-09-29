"""The review mailbox (Phase 4).

Aurelia's functions (Parallax first) propose changes here; nothing they infer
changes her behaviour until Kentrell decides. Deciding happens only through
these endpoints, which the display calls when he presses a button. There is no
tool for it, so Aurelia can show the mailbox but can never approve her own ideas.

Decisions:
- approve: the proposal takes effect (see APPLY) and is logged
- modify:  approved with his changes (validated per kind)
- reject:  the reason is kept; the same idea isn't proposed again unless its
           evidence grows by half or more
- defer:   hidden until a date, then back in the mailbox; expires if left too long
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import db
from .events import PanelEvent, publish

log = logging.getLogger("aurelia.mailbox")
router = APIRouter(prefix="/mailbox", tags=["mailbox"])

REOPEN_FACTOR = 1.5          # a rejected idea returns only with this much more evidence
DEFER_DAYS = 7
EXPIRE_AFTER_DAYS = 60       # deferred ideas nobody came back to
LEVELS = ("normal", "high", "critical")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class Proposal(BaseModel):
    kind: str
    fingerprint: str
    title: str
    summary: str = ""
    proposal: dict[str, Any]
    evidence: list[str] = []
    strength: float = 0
    confidence: float = 0
    effect: str = ""
    urgent: bool = False
    source: str = "parallax"


class Decision(BaseModel):
    action: str                         # approve | modify | reject | defer
    changes: dict[str, Any] = {}        # modify: the fields to change
    reason: str | None = None           # reject: why (optional)
    days: int = DEFER_DAYS              # defer: for how long
    user: str = "owner"


class Batch(BaseModel):
    ids: list[int]
    action: str                         # approve | reject | defer
    user: str = "owner"


# ---- validating and applying ---------------------------------------------------------

def validate(kind: str, proposal: dict[str, Any]) -> dict[str, Any]:
    """The proposal as it will be applied, or HTTP 400 in plain words."""
    p = dict(proposal)
    if not p.get("tool"):
        raise HTTPException(400, "The proposal doesn't say which tool it's about.")
    if kind == "criticality":
        if p.get("level") not in LEVELS:
            raise HTTPException(400, f"Level must be one of: {', '.join(LEVELS)}.")
    elif kind == "retention":
        try:
            p["hot_days"] = int(p.get("hot_days"))
        except (TypeError, ValueError):
            raise HTTPException(400, "Keep-for must be a number of days.")
        if not 1 <= p["hot_days"] <= 365:
            raise HTTPException(400, "Keep full panels for 1 to 365 days.")
    elif kind == "automation":
        s = dict(p.get("schedule") or {})
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(s.get("time", ""))):
            raise HTTPException(400, "Time must look like 07:30.")
        days = [str(d).lower()[:3] for d in (s.get("days") or [])]
        if not days or any(d not in DAYS for d in days):
            raise HTTPException(400, "Days must be some of: mon tue wed thu fri sat sun.")
        s["days"] = [d for d in DAYS if d in days]
        p["schedule"] = s
        p.setdefault("args", {})
    else:
        raise HTTPException(400, f"I don't know how to apply a '{kind}' proposal.")
    return p


async def apply(conn, item_id: int, kind: str, p: dict[str, Any]) -> str:
    """Make an approved proposal real. Returns what happened, in plain words."""
    if kind == "criticality":
        await conn.execute(
            "INSERT INTO tool_criticality (tool, level, reason, from_item) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (tool) DO UPDATE SET level=EXCLUDED.level, reason=EXCLUDED.reason, "
            "from_item=EXCLUDED.from_item, set_at=now()", p["tool"], p["level"], p.get("reason"), item_id)
        global _levels_at
        _levels_at = 0.0                                # take effect on the next task, not in a minute
        return f"{p['tool']} is now {p['level']}."
    if kind == "retention":
        await conn.execute(
            "INSERT INTO tool_retention (tool, hot_days, from_item) VALUES ($1, $2, $3) "
            "ON CONFLICT (tool) DO UPDATE SET hot_days=EXCLUDED.hot_days, from_item=EXCLUDED.from_item, set_at=now()",
            p["tool"], p["hot_days"], item_id)
        return f"Full {p['tool']} panels are now kept for {p['hot_days']} days."
    if kind == "automation":
        await conn.execute(
            "INSERT INTO automations (tool, args, schedule, from_item) VALUES ($1, $2::jsonb, $3::jsonb, $4)",
            p["tool"], json.dumps(p.get("args") or {}), json.dumps(p["schedule"]), item_id)
        return "Saved. It will run on schedule once Pulsar (Phase 10) is built."
    raise HTTPException(400, f"I don't know how to apply a '{kind}' proposal.")


# ---- reading ---------------------------------------------------------------------------

async def _wake_and_expire() -> None:
    """Deferred items come back when their date passes; long-forgotten ones expire."""
    await db.pool().execute("UPDATE mailbox_items SET status='waiting', defer_until=NULL, updated_at=now() "
                            "WHERE status='deferred' AND defer_until <= now()")
    await db.pool().execute("UPDATE mailbox_items SET status='expired', updated_at=now() WHERE status IN "
                            "('deferred','waiting') AND created_at < now() - make_interval(days => $1)",
                            EXPIRE_AFTER_DAYS)


async def counts() -> dict[str, int]:
    await _wake_and_expire()
    row = await db.pool().fetchrow("SELECT count(*) FILTER (WHERE status='waiting') AS waiting, "
                                   "count(*) FILTER (WHERE status='waiting' AND urgent) AS urgent FROM mailbox_items")
    return {"waiting": row["waiting"], "urgent": row["urgent"]}


async def notify(redis) -> None:
    """Tell displays the count changed (the chip) and refresh an open mailbox panel."""
    await publish(redis, PanelEvent(type="mailbox.changed", task_id="mailbox", tool="mailbox", data=await counts()))


def _age(ts: datetime) -> str:
    s = int((datetime.now(timezone.utc) - ts).total_seconds())
    return "just now" if s < 60 else f"{s // 60} min ago" if s < 3600 else f"{s // 3600} h ago" if s < 86400 else f"{s // 86400} d ago"


def view(r: dict[str, Any]) -> dict[str, Any]:
    load = lambda v: json.loads(v) if isinstance(v, str) else v
    return {"id": r["id"], "kind": r["kind"], "title": r["title"], "summary": r["summary"],
            "proposal": load(r["proposal"]), "evidence": load(r["evidence"]), "confidence": round(r["confidence"], 2),
            "effect": r["effect"], "status": r["status"], "urgent": r["urgent"], "age": _age(r["created_at"]),
            "reason": r["reason"], "applied": load(r["applied"]) if r["applied"] is not None else None}


@router.get("")
async def list_items(status: str = "waiting", limit: int = 50):
    await _wake_and_expire()
    rows = await db.pool().fetch(
        "SELECT * FROM mailbox_items WHERE ($1 = 'all' OR status = $1) ORDER BY urgent DESC, created_at DESC LIMIT $2",
        status, min(max(limit, 1), 200))
    return {"items": [view(dict(r)) for r in rows], "counts": await counts()}


@router.get("/count")
async def count():
    return await counts()


@router.get("/{item_id}")
async def get_item(item_id: int):
    row = await db.pool().fetchrow("SELECT * FROM mailbox_items WHERE id=$1", item_id)
    if row is None:
        raise HTTPException(404, "No such mailbox item.")
    return view(dict(row))


# ---- proposing ------------------------------------------------------------------------

@router.post("/proposals")
async def propose(p: Proposal, request: Request):
    """Functions propose here. The same idea (fingerprint) is updated, not duplicated;
    a decided idea isn't proposed again unless its evidence has grown a lot."""
    validate(p.kind, p.proposal)
    pool = db.pool()
    await _wake_and_expire()
    last = await pool.fetchrow("SELECT * FROM mailbox_items WHERE fingerprint=$1 ORDER BY created_at DESC LIMIT 1",
                               p.fingerprint)
    fields = (p.title, p.summary, json.dumps(p.proposal), json.dumps(p.evidence), p.strength, p.confidence, p.effect, p.urgent)
    if last is not None:
        status = last["status"]
        if status in ("waiting", "deferred"):
            await pool.execute(
                "UPDATE mailbox_items SET title=$2, summary=$3, proposal=$4::jsonb, evidence=$5::jsonb, strength=$6, "
                "confidence=$7, effect=$8, urgent=$9, updated_at=now() WHERE id=$1", last["id"], *fields)
            return {"id": last["id"], "result": "updated"}
        if status in ("approved", "modified"):
            return {"id": last["id"], "result": "already_approved"}
        if status == "rejected" and p.strength < last["strength"] * REOPEN_FACTOR:
            return {"id": last["id"], "result": "declined_before"}
    summary = p.summary
    if last is not None and last["status"] == "rejected":
        why = f" (you said: {last['reason']})" if last["reason"] else ""
        summary = f"{summary} I'm asking again because the evidence has grown since you declined this{why}."
    item_id = await pool.fetchval(
        "INSERT INTO mailbox_items (kind, fingerprint, source, title, summary, proposal, evidence, strength, confidence, "
        "effect, urgent) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9, $10, $11) RETURNING id",
        p.kind, p.fingerprint, p.source, p.title, summary, *fields[2:])
    log.info("mailbox: new %s proposal %s from %s", p.kind, item_id, p.source)
    await notify(request.app.state.redis)
    return {"id": item_id, "result": "created"}


# ---- deciding (Kentrell only, from the display) ----------------------------------------

async def _decide(item_id: int, d: Decision) -> dict[str, Any]:
    pool = db.pool()
    row = await pool.fetchrow("SELECT * FROM mailbox_items WHERE id=$1", item_id)
    if row is None:
        raise HTTPException(404, "No such mailbox item.")
    if row["status"] not in ("waiting", "deferred"):
        raise HTTPException(409, f"That was already {row['status']}.")
    proposal = json.loads(row["proposal"]) if isinstance(row["proposal"], str) else row["proposal"]
    if d.action in ("approve", "modify"):
        final = validate(row["kind"], {**proposal, **d.changes} if d.action == "modify" else proposal)
        async with pool.acquire() as conn, conn.transaction():
            done = await apply(conn, item_id, row["kind"], final)
            await conn.execute("UPDATE mailbox_items SET status=$2, applied=$3::jsonb, decided_at=now(), decided_by=$4, "
                               "updated_at=now() WHERE id=$1", item_id,
                               "modified" if d.action == "modify" else "approved", json.dumps(final), d.user)
        return {"id": item_id, "status": "modified" if d.action == "modify" else "approved", "done": done}
    if d.action == "reject":
        await pool.execute("UPDATE mailbox_items SET status='rejected', reason=$2, decided_at=now(), decided_by=$3, "
                           "updated_at=now() WHERE id=$1", item_id, (d.reason or "").strip() or None, d.user)
        return {"id": item_id, "status": "rejected", "done": "Declined. I won't suggest it again unless things change a lot."}
    if d.action == "defer":
        until = datetime.now(timezone.utc) + timedelta(days=max(1, min(d.days, 60)))
        await pool.execute("UPDATE mailbox_items SET status='deferred', defer_until=$2, updated_at=now() WHERE id=$1",
                           item_id, until)
        return {"id": item_id, "status": "deferred", "done": f"I'll bring it back in {max(1, min(d.days, 60))} days."}
    raise HTTPException(400, "Action must be approve, modify, reject or defer.")


@router.post("/{item_id}/decide")
async def decide(item_id: int, d: Decision, request: Request):
    out = await _decide(item_id, d)
    await notify(request.app.state.redis)
    return out


@router.post("/decide-batch")
async def decide_batch(b: Batch, request: Request):
    """One decision for several similar items (e.g. 'approve all 3 automations')."""
    if b.action not in ("approve", "reject", "defer"):
        raise HTTPException(400, "Batch action must be approve, reject or defer.")
    results = []
    for item_id in b.ids:
        try:
            results.append(await _decide(item_id, Decision(action=b.action, user=b.user)))
        except HTTPException as exc:
            results.append({"id": item_id, "error": exc.detail})
    await notify(request.app.state.redis)
    return {"results": results}


# ---- used elsewhere -------------------------------------------------------------------

_levels: dict[str, str] = {}
_levels_at = 0.0


async def criticality(tool: str) -> str:
    """This tool's approved level (cached for a minute)."""
    import time
    global _levels, _levels_at
    if time.monotonic() - _levels_at > 60:
        rows = await db.pool().fetch("SELECT tool, level FROM tool_criticality")
        _levels, _levels_at = {r["tool"]: r["level"] for r in rows}, time.monotonic()
    return _levels.get(tool, "normal")
