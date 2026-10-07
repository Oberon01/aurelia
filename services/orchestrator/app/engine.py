"""Admiral's scheduling engine: timers that survive restarts.

A job is what to run, when, and its rules:
  - what: a tool call (through governance, like anything he asks), an Admiral plan, a reminder, or one of
    her own system jobs (a handler in SYSTEM, e.g. memory maintenance, or watching a computer come back);
  - when: once, every N minutes, or on days at a time; in his time zone, through daylight saving;
  - rules: how late a missed run may still run (grace), and a heads-up with a Cancel window first for
    physical actions (governance: admiral rules.heads_up_for).

Jobs live in Postgres. One runner per orchestrator claims due jobs with a lease (FOR UPDATE SKIP LOCKED), so
two orchestrators during a zero-downtime deploy never both run one, and a job cut off by a restart is
claimed again when its lease runs out: a tool job whose task had started waits for that task's own result
instead of running it twice. A run late by more than its grace is marked missed and he's told; a backlog
never runs more than once. Each run is checked by its own result (the same rule as plan steps) and logged in
job_runs. Governance is applied when a job runs, not when it's scheduled: a change-tier action asks him then
(a mailbox card and his phone), and an OK that comes after the grace is too late to run it.
"""
import asyncio
import json
import logging
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request

from . import auth, db, when

log = logging.getLogger("aurelia.engine")
router = APIRouter(prefix="/admiral/jobs", tags=["admiral"])

LEASE = 120                       # seconds a claim lasts; renewed while a job runs
TICK = 2.0                        # longest the runner sleeps between looks
STEP_WAIT = 600                   # how long a scheduled tool may run before it's called failed
ME = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"   # unique per process, even as pid 1
_wake = asyncio.Event()

RULES = {
    "grace_minutes": {"action": 10, "plan": 60, "reminder": 360},
    "heads_up_seconds": 60,
    "heads_up_for": ["orbit.lock", "orbit.unlock", "orbit.light", "orbit.thermostat",
                     "constellation.power", "constellation.restart", "constellation.wake"],
    "retry_after_seconds": 120,
    "remind_waiting_hours": 4,
    "expire_waiting_days": 3,
}


def rules(app) -> dict:
    pol = app.state.governance.functions.get("admiral")
    got = ((pol.rules if pol else None) or {}).get("schedule") or {}
    out = {**RULES, **got}
    out["grace_minutes"] = {**RULES["grace_minutes"], **(got.get("grace_minutes") or {})}
    return out


def tz_name(app) -> str:
    from . import budget
    return budget.tz_name(app)


def now() -> datetime:
    return datetime.now(timezone.utc)


# ---- system jobs: her own timers ------------------------------------------------------------------------
# name -> (handler(app, spec) -> {"again_in": s, "spec": {...}} | None, every(app) -> minutes | None)

Handler = Callable[[Any, dict], Awaitable[dict | None]]
SYSTEM: dict[str, tuple[Handler, Callable[[Any], float] | None]] = {}


def system(name: str, every: Callable[[Any], float] | None = None):
    """Register one of her own jobs. With every: it recurs (one copy, made at start-up)."""
    def deco(fn: Handler) -> Handler:
        SYSTEM[name] = (fn, every)
        return fn
    return deco


async def ensure_system_jobs(app) -> None:
    """Each recurring system job exists once (two orchestrators starting together make one)."""
    for name, (_, every) in SYSTEM.items():
        if every is None:
            continue
        exists = await db.pool().fetchval(
            "SELECT 1 FROM jobs WHERE created_by='system' AND name=$1 AND status IN ('scheduled','running') "
            "AND rule ? 'every_minutes'", name)
        if exists:
            continue
        mins = max(0.05, float(every(app)))
        first = now() + timedelta(seconds=min(60, mins * 60))
        try:
            await db.pool().execute(
                "INSERT INTO jobs (kind, name, title, created_by, rule, tz, due_at, next_at) "
                "VALUES ('system', $1, $2, 'system', $3::jsonb, $4, $5, $5)",
                name, name, json.dumps({"every_minutes": mins}), tz_name(app), first)
        except Exception as exc:                        # the other orchestrator made it first
            if "jobs_system_name" not in str(exc):
                raise


async def after(app, seconds: float, name: str, spec: dict, title: str | None = None) -> int:
    """One of her own follow-ups, durable: run SYSTEM[name] with spec in `seconds` (it may ask to go again)."""
    at = now() + timedelta(seconds=max(0.0, seconds))
    job = await db.pool().fetchval(
        "INSERT INTO jobs (kind, name, title, created_by, spec, rule, tz, due_at, next_at) "
        "VALUES ('system', $1, $2, 'system', $3::jsonb, '{\"once\": true}'::jsonb, $4, $5, $5) RETURNING id",
        name, title or name, json.dumps(spec), tz_name(app), at)
    poke()
    return job


def poke() -> None:
    """A new or changed job: look again now instead of at the next tick."""
    _wake.set()


# ---- his jobs ---------------------------------------------------------------------------------------------

def heads_for(app, kind: str, spec: dict) -> bool:
    """Physical actions get a heads-up with a Cancel first: a command on the list, or a routine with one in it."""
    listed = set(rules(app)["heads_up_for"])
    if kind == "tool":
        return spec.get("tool") in listed
    if kind == "routine":
        return any(t in listed for t in spec.get("tools") or [])
    return False


async def add(app, kind: str, title: str, spec: dict, at: datetime, user: str = "owner",
              device: int | None = None, rule: dict | None = None, conn=None) -> dict:
    """Schedule something he asked for (once, or one of his automations). Physical actions get a heads-up first.
    An automation that waits for an event (rule "on") is 'armed': it runs only when its event happens."""
    r = rules(app)
    heads = heads_for(app, kind, spec)
    grace = int(float(r["grace_minutes"]["action" if kind in ("tool", "routine") else kind]) * 60)
    first = max(now(), at - timedelta(seconds=int(r["heads_up_seconds"]))) if heads else at
    armed = bool((rule or {}).get("on"))
    row = await (conn or db.pool()).fetchrow(
        "INSERT INTO jobs (kind, title, user_id, spec, rule, tz, planned_at, due_at, next_at, phase, heads_up, "
        "grace_seconds, device, status) VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7, $7, $8, $9, $10, $11, $12, $13) "
        "RETURNING *",
        kind, title[:200], user, json.dumps(spec), json.dumps(rule or {"once": True}), tz_name(app), at, first,
        "heads_up" if heads else "run", heads, grace, device, "armed" if armed else "scheduled")
    poke()
    return dict(row)


def _j(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


def view(job: dict, at: datetime | None = None) -> dict:
    tz = ZoneInfo(job.get("tz") or "America/Chicago")
    due = (job.get("planned_at") or job["due_at"]).astimezone(tz)       # the time he was told
    n = (at or now()).astimezone(tz)
    left = due - n
    mins = int(left.total_seconds() // 60)
    rel = ("now" if left.total_seconds() < 60 else f"in {mins} min" if mins < 60 else
           f"in {mins // 60} h {mins % 60} min" if mins < 24 * 60 else f"in {left.days} days")
    spec = _j(job.get("spec"))
    out = {"id": job["id"], "title": job["title"], "kind": job["kind"], "when": when.say(due, n), "in": rel,
           "at": due.isoformat(), "status": job["status"], "heads_up": job.get("heads_up", False)}
    if job["kind"] == "tool":
        out["tool"] = spec.get("tool")
        out["args"] = spec.get("args") or {}
    if job["kind"] == "routine":
        out["routine"] = spec.get("name")
    rep = repeats(job)
    if rep:
        out["repeats"] = rep
        if job["status"] in ("armed", "paused"):          # no next time by the clock
            out["when"], out["in"] = ("waiting for that" if job["status"] == "armed" else "paused"), ""
    return out


async def cancel(app, job_id: int, why: str = "cancelled") -> dict:
    """Cancel one of his jobs (before it runs; during its heads-up too). An automation's Cancel skips this time
    only (deleting it is Pulsar's 'delete'): the result says so with "skipped" and the next time."""
    from . import mailbox
    j = await db.pool().fetchrow("SELECT * FROM jobs WHERE id=$1 AND created_by='owner'", job_id)
    if j is not None and recurring(dict(j)):
        return await skip(app, job_id, why)
    row = await db.pool().fetchrow(
        "UPDATE jobs SET status='cancelled', last_outcome=$2, updated_at=now() WHERE id=$1 AND created_by='owner' "
        "AND status IN ('scheduled','waiting') RETURNING *", job_id, why)
    if row is None:
        st = await db.pool().fetchval("SELECT status FROM jobs WHERE id=$1 AND created_by='owner'", job_id)
        if st is None:
            raise ValueError("Nothing like that is scheduled.")
        raise ValueError("It's already running." if st == "running" else f"That one is already {st}.")
    if row["mailbox_id"]:
        if await db.pool().fetchval("UPDATE mailbox_items SET status='expired', updated_at=now() WHERE id=$1 "
                                    "AND status IN ('waiting','deferred') RETURNING id", row["mailbox_id"]):
            await mailbox.notify(app.state.redis)
    await _close_heads_up(app, row["id"])
    await _log(row["id"], row["planned_at"] or row["due_at"], "cancelled", None, why)
    return dict(row)


@router.get("/{job_id}/panel")
async def job_panel(job_id: int):
    """What a job's notification opens: its run's panel lives at /tasks; a reminder is rebuilt here."""
    row = await db.pool().fetchrow("SELECT * FROM jobs WHERE id=$1", job_id)
    if row is None:
        raise HTTPException(404, "No such job.")
    job = dict(row)
    due = when.say((job.get("planned_at") or job["due_at"]).astimezone(ZoneInfo(job["tz"])), now())
    if job["kind"] == "reminder":
        text = _j(job["spec"]).get("text") or job["title"]
        return {"task_id": f"job-{job_id}", "tool": "admiral.reminder",
                "data": {"status": "completed", "partial": False, "headline": text,
                         "reminder": {"text": text, "job": job_id, "when": due}}}
    return {"task_id": f"job-{job_id}", "tool": "admiral.scheduled",
            "data": {"status": "completed", "partial": False, "headline": f"{job['title']}: {job['last_outcome'] or job['status']}",
                     "scheduled": {"job": view(job)}}}


@router.post("/{job_id}/cancel")
async def cancel_http(job_id: int, request: Request):
    dev = await auth.asker(request.headers)
    if dev is not None and dev["kind"] == "screen":
        raise HTTPException(403, "A TV's screen can't cancel that.")
    try:
        row = await cancel(request.app, job_id, "you cancelled it")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    if row.get("skipped"):
        return {"ok": True, "done": f"Skipped this time: {row['title']}. {row['skipped']}"}
    return {"ok": True, "done": f"Cancelled: {row['title']}."}


# ---- automations: the next time, skipping one, pausing (Pulsar) ------------------------------------------

def slot(app, job: dict, after: datetime) -> tuple[str, datetime, datetime, str]:
    """An automation's next occurrence after `after`: (status, planned, next look, phase). An event's automation
    goes back to waiting for its event ('armed')."""
    rule = _j(job["rule"])
    if rule.get("on"):
        return "armed", now(), now(), "run"
    nxt = next_time(rule, after, job["tz"])
    if nxt is None:
        raise ValueError("That automation has no next time.")
    hs = int(rules(app)["heads_up_seconds"]) if job.get("heads_up") else 0
    return "scheduled", nxt, nxt - timedelta(seconds=hs), "heads_up" if hs else "run"


def next_said(app, job: dict, status: str, planned: datetime) -> str:
    if status == "armed":
        return f"It runs again {repeats(job)}."
    return f"Next: {when.say(planned.astimezone(ZoneInfo(job['tz'])), now())}."


async def skip(app, job_id: int, why: str = "you skipped it", only: tuple[str, ...] = ("scheduled", "waiting")) -> dict:
    """Skip an automation's next run (also its Cancel during a heads-up, and his 'no' on its card): it carries on
    from the occurrence after."""
    from . import mailbox
    async with db.pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT * FROM jobs WHERE id=$1 AND created_by='owner' FOR UPDATE", job_id)
        if row is None:
            raise ValueError("Nothing like that is scheduled.")
        job = dict(row)
        if job["status"] not in only:
            if job["status"] in ("scheduled", "waiting"):
                raise ValueError("That isn't waiting for you anymore.")
            raise ValueError("It's running right now." if job["status"] == "running" else
                             f"That one is {job['status']}, so there's no next run to skip.")
        status, planned, nxt, phase = slot(app, job, max(_planned(job), now()))
        await conn.execute(
            "UPDATE jobs SET status=$2, planned_at=$3, due_at=$3, next_at=$4, phase=$5, task_id=NULL, tries=0, "
            "mailbox_id=NULL, last_outcome='skipped', lease_until=NULL, spec = spec - 'approved', updated_at=now() "
            "WHERE id=$1",
            job_id, status, planned, nxt, phase)
    if job["mailbox_id"]:
        if await db.pool().fetchval("UPDATE mailbox_items SET status='expired', updated_at=now() WHERE id=$1 "
                                    "AND status IN ('waiting','deferred') RETURNING id", job["mailbox_id"]):
            await mailbox.notify(app.state.redis)
    await _close_heads_up(app, job_id)
    await _log(job_id, _planned(job), "skipped", None, why)
    poke()
    return {**job, "skipped": next_said(app, job, status, planned)}


async def pause(app, job_id: int, why: str) -> dict | None:
    """Stop an automation until he resumes it (his word, or too many failures in a row)."""
    from . import mailbox
    row = await db.pool().fetchrow(
        "UPDATE jobs SET status='paused', lease_until=NULL, spec = spec - 'approved', updated_at=now() WHERE id=$1 "
        "AND created_by='owner' "
        "AND NOT (rule ? 'once') AND status IN ('scheduled','armed','waiting') RETURNING *", job_id)
    if row is None:
        return None
    if row["mailbox_id"] and await db.pool().fetchval(
            "UPDATE mailbox_items SET status='expired', updated_at=now() WHERE id=$1 AND status IN ('waiting','deferred') "
            "RETURNING id", row["mailbox_id"]):
        await mailbox.notify(app.state.redis)
    await _close_heads_up(app, job_id)
    await _log(job_id, _planned(dict(row)), "paused", None, why)
    return dict(row)


async def resume(app, job_id: int) -> dict | None:
    """Back on, from the next time after now (never a backlog of what it missed while paused)."""
    async with db.pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT * FROM jobs WHERE id=$1 AND created_by='owner' AND status='paused' FOR UPDATE",
                                  job_id)
        if row is None:
            return None
        job = dict(row)
        status, planned, nxt, phase = slot(app, job, now())
        await conn.execute(
            "UPDATE jobs SET status=$2, planned_at=$3, due_at=$3, next_at=$4, phase=$5, task_id=NULL, tries=0, "
            "fails_in_row=0, mailbox_id=NULL, spec = spec - 'approved', updated_at=now() WHERE id=$1",
            job_id, status, planned, nxt, phase)
    poke()
    return {**job, "status": status, "planned_at": planned, "due_at": planned, "next": next_said(app, job, status, planned)}


async def fire(app, job_id: int, why: str) -> bool:
    """An armed automation's event happened: it runs now (after its heads-up), at most once per cooldown."""
    hs = int(rules(app)["heads_up_seconds"])
    row = await db.pool().fetchrow(
        "UPDATE jobs SET status='scheduled', spec = spec - 'approved', "
        "planned_at=now() + make_interval(secs => CASE WHEN heads_up THEN $2 ELSE 0 END), "
        "due_at=now() + make_interval(secs => CASE WHEN heads_up THEN $2 ELSE 0 END), next_at=now(), "
        "phase=CASE WHEN heads_up THEN 'heads_up' ELSE 'run' END, task_id=NULL, tries=0, updated_at=now() "
        "WHERE id=$1 AND status='armed' RETURNING id", job_id, float(hs))
    if row is not None:
        log.info("automation %s fired: %s", job_id, why)
        poke()
    return row is not None


# ---- the runner -----------------------------------------------------------------------------------------

async def claim(limit: int = 20) -> list[dict]:
    rows = await db.pool().fetch(
        "WITH c AS (SELECT id, status AS was FROM jobs WHERE (status='scheduled' AND next_at <= now()) "
        "  OR (status='running' AND lease_until < now()) ORDER BY next_at LIMIT $2 FOR UPDATE SKIP LOCKED) "
        "UPDATE jobs j SET status='running', lease_until=now() + make_interval(secs => $3), claimed_by=$1, "
        "updated_at=now() FROM c WHERE j.id = c.id RETURNING j.*, c.was", ME, limit, LEASE)
    return [dict(r) for r in rows]


_live: set[asyncio.Task] = set()             # running jobs, kept referenced until they finish


async def loop(app) -> None:
    await asyncio.sleep(0)
    try:
        await ensure_system_jobs(app)
    except Exception:
        log.exception("couldn't set up her system jobs; will retry")
    while True:
        _wake.clear()                        # before looking, so a poke during the look isn't lost
        try:
            for job in await claim():
                t = asyncio.create_task(_execute(app, job))
                _live.add(t)
                t.add_done_callback(_live.discard)
            nxt = await db.pool().fetchval("SELECT min(next_at) FROM jobs WHERE status='scheduled'")
            wait = TICK if nxt is None else max(0.05, min(TICK, (nxt - now()).total_seconds()))
        except Exception:
            log.exception("engine round failed")
            wait = TICK
        try:
            await asyncio.wait_for(_wake.wait(), wait)
        except asyncio.TimeoutError:
            pass


async def _renew(job_id: int) -> None:
    while True:
        await asyncio.sleep(LEASE / 3)
        try:
            await db.pool().execute("UPDATE jobs SET lease_until=now() + make_interval(secs => $2) WHERE id=$1 "
                                    "AND status='running' AND claimed_by=$3", job_id, LEASE, ME)
        except Exception:
            log.exception("couldn't renew the lease on job %s; trying again", job_id)


class LostLease(Exception):
    """Another runner has this job now (this one was too slow): it finishes it, not us."""


async def _mine(sql: str, job_id: int, *args) -> None:
    """A runner-side update, only while this runner still holds the job."""
    done = await db.pool().fetchval(sql + " AND claimed_by=$%d AND status IN ('running') RETURNING id" % (len(args) + 2),
                                    job_id, *args, ME)
    if done is None:
        raise LostLease()


async def _log(job_id: int, due: datetime, outcome: str, task_id: Any = None, note: str | None = None,
               started: datetime | None = None) -> None:
    late = max(0, int(((started or now()) - due).total_seconds()))
    await db.pool().execute(
        "INSERT INTO job_runs (job_id, due_at, started_at, finished_at, late_seconds, outcome, task_id, note) "
        "VALUES ($1, $2, $3, now(), $4, $5, $6, $7)", job_id, due, started or now(), late, outcome,
        uuid.UUID(str(task_id)) if task_id else None, (note or "")[:500] or None)


def next_time(rule: dict, after: datetime, tz: str, every: float | None = None) -> datetime | None:
    """The next occurrence after `after`: every N minutes (from now, never a backlog), or days at a time."""
    if every is not None or rule.get("every_minutes"):
        return after + timedelta(minutes=float(every if every is not None else rule["every_minutes"]))
    if rule.get("days") and rule.get("time"):
        z = ZoneInfo(tz)
        h, m = (int(x) for x in str(rule["time"]).split(":"))
        days = [when.DAYS.index(d) if len(d) > 3 else ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(d)
                for d in rule["days"]]
        local = after.astimezone(z)
        for add_ in range(0, 8):
            d = local.date() + timedelta(days=add_)
            if d.weekday() not in days:
                continue
            at = when.localize(datetime(d.year, d.month, d.day, h, m), z)
            if at > after:
                return at
    if rule.get("monthday") and rule.get("time"):           # 31 (or more than the month has) = its last day
        import calendar
        z = ZoneInfo(tz)
        h, m = (int(x) for x in str(rule["time"]).split(":"))
        local = after.astimezone(z)
        y, mo = local.year, local.month
        for _ in range(3):
            day = min(int(rule["monthday"]), calendar.monthrange(y, mo)[1])
            at = when.localize(datetime(y, mo, day, h, m), z)
            if at > after:
                return at
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    return None                                             # once, or an event's ("on"): no next time by the clock


def recurring(job: dict) -> bool:
    """One of his automations (Pulsar): it repeats, by the clock or on an event, until he deletes it."""
    return job.get("created_by", "owner") == "owner" and not _j(job.get("rule")).get("once")


def repeats(job: dict) -> str | None:
    """How an automation repeats, in his words ('every weekday at 7:00 AM', 'when Den PC comes online')."""
    rule = _j(job.get("rule"))
    if rule.get("once"):
        return None
    if rule.get("on") in ("state", "hook"):
        from .happenings import says
        return says(rule)
    if rule.get("on"):
        from .happenings import say_conditions
        what = {"online": "comes online", "offline": "goes offline", "open": "opens"}.get(rule["on"], rule["on"])
        return f"when {rule.get('device_name') or 'it'} {what}{say_conditions(rule)}"
    return when.say_rule(rule)


def _planned(job: dict) -> datetime:
    return job.get("planned_at") or job["due_at"]


async def _finish(app, job: dict, outcome: str, note: str | None = None, task_id: Any = None,
                  spec: dict | None = None, again_in: float | None = None) -> None:
    """End this run: schedule the next occurrence (or a system job's follow-up), or end the job; then log it.
    Raises LostLease if another runner holds the job now (it records the run instead)."""
    rule = _j(job["rule"])
    if recurring(job) and again_in is None and await db.pool().fetchval(
            "SELECT spec ? 'deleted' FROM jobs WHERE id=$1", job["id"]):          # he deleted it while it ran
        await _mine("UPDATE jobs SET status='cancelled', runs=runs+1, last_outcome=$2, last_run_at=now(), lease_until=NULL, "
                    "updated_at=now() WHERE id=$1", job["id"], outcome)
        await _log(job["id"], _planned(job), outcome, task_id or job.get("task_id"), note, job.get("_started"))
        return
    every = None
    if job["kind"] == "system" and job.get("name") in SYSTEM and SYSTEM[job["name"]][1] and rule.get("every_minutes"):
        every = max(0.05, float(SYSTEM[job["name"]][1](app)))          # config can change between runs
    nxt = now() + timedelta(seconds=again_in) if again_in is not None else (
        None if rule.get("once") else next_time(rule, now(), job["tz"], every))
    auto = recurring(job) and again_in is None
    if auto and outcome != "waiting":                   # his automations: failures in a row (one that works resets it)
        await _mine("UPDATE jobs SET fails_in_row = CASE WHEN $2 IN ('failed','unconfirmed') THEN fails_in_row + 1 "
                    "WHEN $2 = 'done' THEN 0 ELSE fails_in_row END WHERE id=$1", job["id"], outcome)
    if outcome == "waiting":
        await _mine("UPDATE jobs SET status='waiting', last_outcome=$2, lease_until=NULL, updated_at=now() WHERE id=$1",
                    job["id"], outcome)
    elif auto and rule.get("on"):                       # an event's automation: back to waiting for its event
        await _mine("UPDATE jobs SET status=CASE WHEN spec ? 'deleted' THEN 'cancelled' ELSE 'armed' END, "
                    "task_id=NULL, tries=0, runs=runs+1, last_outcome=$2, "
                    "last_run_at=now(), lease_until=NULL, mailbox_id=NULL, phase='run', spec = spec - 'approved', updated_at=now() "
                    "WHERE id=$1",
                    job["id"], outcome)
    elif nxt is not None:
        hs = int(rules(app)["heads_up_seconds"]) if job.get("heads_up") else 0
        new_spec = {k: v for k, v in (spec if spec is not None else _j(job["spec"])).items() if k != "approved"}
        await _mine("UPDATE jobs SET status=CASE WHEN spec ? 'deleted' THEN 'cancelled' ELSE 'scheduled' END, "
                    "planned_at=$2, due_at=$2, next_at=$3, phase=$4, task_id=NULL, "
                    "tries=0, runs=runs+1, last_outcome=$5, last_run_at=now(), lease_until=NULL, "
                    "spec=CASE WHEN spec ? 'deleted' THEN spec ELSE $6::jsonb END, "
                    "mailbox_id=NULL, updated_at=now() WHERE id=$1", job["id"], nxt, nxt - timedelta(seconds=hs),
                    "heads_up" if hs else "run", outcome, json.dumps(new_spec))
    else:
        final = {"done": "done", "unconfirmed": "done", "failed": "failed", "missed": "missed"}
        await _mine("UPDATE jobs SET status=$2, runs=runs+1, last_outcome=$3, last_run_at=now(), lease_until=NULL, "
                    "updated_at=now() WHERE id=$1", job["id"], final.get(outcome, "done"), outcome)
    await _log(job["id"], _planned(job), outcome, task_id or job.get("task_id"), note, job.get("_started"))
    if auto and outcome in ("failed", "unconfirmed"):
        await _too_many_failures(app, job)


async def _too_many_failures(app, job: dict) -> None:
    """An automation that failed (or couldn't be confirmed) several times in a row is paused, and he's told."""
    from . import pulsar
    limit = pulsar.rules(app)["pause_after_failures"]
    fails = await db.pool().fetchval("SELECT fails_in_row FROM jobs WHERE id=$1", job["id"]) or 0
    if fails < limit:
        return
    if await pause(app, job["id"], f"{fails} failures in a row") is None:
        return
    last = await db.pool().fetchval("SELECT note FROM job_runs WHERE job_id=$1 AND outcome IN ('failed','unconfirmed') "
                                    "ORDER BY id DESC LIMIT 1", job["id"])
    await pulsar.notice(app, "automation_paused", f"job:{job['id']}:{job['runs']}",
                        f"Paused an automation: {job['title']}",
                        f"It didn't work {fails} times in a row" + (f" (last: {last})" if last else "") +
                        ". Say \"resume the automation\" once it's fixed.")


def _approved(job: dict, spec: dict) -> bool:
    """His OK on a change-tier job's card is for that one occurrence (the time it was planned for): a retry of
    it is still approved; the next run, a skipped one, or one after a pause or resume asks again."""
    a = spec.get("approved")
    if a is True:                                        # (from before approvals carried their time)
        return not recurring(job)
    try:
        return abs(float(a) - _planned(job).timestamp()) < 1
    except (TypeError, ValueError):
        return False


def _late(job: dict) -> float:
    return (now() - _planned(job)).total_seconds()


async def _execute(app, job: dict) -> None:
    renew = asyncio.create_task(_renew(job["id"]))
    job["_started"] = now()
    try:
        if recurring(job) and _j(job["spec"]).get("deleted") and not (job.get("task_id") and await _started(job["task_id"])):
            await _close_heads_up(app, job["id"])        # deleted while it ran: its retry or next heads-up never happens
            await _mine("UPDATE jobs SET status='cancelled', last_outcome='deleted', lease_until=NULL, updated_at=now() "
                        "WHERE id=$1", job["id"])
            await _log(job["id"], _planned(job), "deleted", None, "you deleted it")
            return
        late = _late(job)
        started = bool(job.get("task_id")) and await _started(job["task_id"])     # a started task is waited for
        if job["grace_seconds"] is not None and late > job["grace_seconds"] and not started:
            due = when.say(_planned(job).astimezone(ZoneInfo(job["tz"])))
            await _close_heads_up(app, job["id"])
            await _finish(app, job, "missed", f"due {due}; I was {int(late // 60)} min late")
            await _tell(app, job, "Missed", f"{job['title']} was due {due}, and I couldn't do it in time.")
            return
        if job["phase"] == "heads_up":                  # (again after a restart: a second heads-up beats none)
            await _heads_up(app, job)
            return
        if job["kind"] == "system":
            await _run_system(app, job)
        elif job["kind"] == "reminder":
            await _remind(app, job, late)
        elif job["kind"] == "tool":
            await _run_tool(app, job)
        elif job["kind"] == "plan":
            await _run_plan(app, job)
        elif job["kind"] == "routine":
            await _run_routine(app, job)
        else:
            await _finish(app, job, "failed", f"unknown kind {job['kind']}")
    except LostLease:
        log.warning("job %s (%s): another runner has it now", job["id"], job["title"])
    except Exception as exc:
        log.exception("job %s (%s) failed", job["id"], job["title"])
        try:
            await _finish(app, job, "failed", f"Something went wrong: {exc}"[:300])
            if job["created_by"] == "owner":
                await _tell(app, job, "That didn't work", f"{job['title']}: {exc}"[:200])
        except LostLease:
            pass
        except Exception:
            log.exception("couldn't record job %s", job["id"])
    finally:
        renew.cancel()


async def _run_system(app, job: dict) -> None:
    entry = SYSTEM.get(job["name"] or "")
    if entry is None:
        await _finish(app, job, "failed", f"no handler {job['name']}")
        return
    out = await entry[0](app, _j(job["spec"]))
    if out and out.get("again_in") is not None:
        await _finish(app, job, "done", out.get("note"), spec=out.get("spec"), again_in=float(out["again_in"]))
    else:
        await _finish(app, job, "done", (out or {}).get("note"))


# ---- what each kind of job does when it's due ------------------------------------------------------------

async def _tell(app, job: dict, title: str, body: str, panel: dict | None = None) -> None:
    """His phone (wherever it isn't on screen) and, with a panel, a panel on his displays. The notification opens
    what it's about: the run's own panel, or the reminder."""
    from . import push
    from .events import PanelEvent, publish
    link = f"/view?task={job['task_id']}" if job.get("task_id") else f"/view?job={job['id']}"
    try:
        await push.notify(app, title, body, link, f"job-{job['id']}")
    except Exception:
        log.exception("job notification")
    if panel is not None:
        tid = f"job-{job['id']}-{job.get('runs', 0)}"
        await publish(app.state.redis, PanelEvent(type="panel.open", task_id=tid, tool=panel.pop("_tool"),
                                                  data={"tier": "reference", "args": {}}))
        await publish(app.state.redis, PanelEvent(type="panel.result", task_id=tid, tool=panel.get("_t", "admiral.job"),
                                                  data={"status": "completed", "partial": False, **panel}))


async def _remind(app, job: dict, late: float) -> None:
    text = _j(job["spec"]).get("text") or job["title"]
    due = when.say(_planned(job).astimezone(ZoneInfo(job["tz"])), now())
    note = f"(late: it was due {due})" if late > 120 else None
    await _finish(app, job, "done", note)
    await _tell(app, job, "Reminder", text + (f" {note}" if note else ""),
                {"_tool": "admiral.reminder", "_t": "admiral.reminder", "headline": text,
                 "reminder": {"text": text, "job": job["id"], "when": due, **({"late": note} if note else {})}})


async def _heads_up(app, job: dict) -> None:
    hs = int(rules(app)["heads_up_seconds"])
    run_at = max(job["due_at"], now() + timedelta(seconds=hs))
    from .events import PanelEvent, publish
    from . import push
    await _mine("UPDATE jobs SET phase='run', due_at=$2, next_at=$2, status='scheduled', lease_until=NULL, "
                "updated_at=now() WHERE id=$1", job["id"], run_at)
    tid = f"headsup-{job['id']}"
    data = {"headline": f"In {hs // 60 or 1} minute{'s' if hs >= 120 else ''}: {job['title']}" if hs >= 60
            else f"In {hs} seconds: {job['title']}",
            "heads_up": {"job": job["id"], "title": job["title"], "at": run_at.isoformat(), "seconds": hs}}
    await publish(app.state.redis, PanelEvent(type="panel.open", task_id=tid, tool="admiral.heads_up",
                                              data={"tier": "reference", "args": {}}))
    await publish(app.state.redis, PanelEvent(type="panel.result", task_id=tid, tool="admiral.heads_up",
                                              data={"status": "completed", "partial": False, **data}))
    try:
        await push.notify(app, data["headline"], "Open Aurelia to cancel it.", "/view", f"job-{job['id']}")
    except Exception:
        log.exception("heads-up notification")
    poke()


async def _close_heads_up(app, job_id: int) -> None:
    from .events import PanelEvent, publish
    await publish(app.state.redis, PanelEvent(type="panel.close", task_id=f"headsup-{job_id}", tool="admiral.heads_up",
                                              data={"status": "completed", "has_result": False}))


async def _card(app, job: dict, reason: str) -> int:
    """A change-tier job, due now: his OK, on a mailbox card (and his phone)."""
    from . import mailbox
    spec = _j(job["spec"])
    what = ", ".join(f"{k}: {v if isinstance(v, str) else json.dumps(v)}" for k, v in (spec.get("args") or {}).items())
    item = await db.pool().fetchval(
        "INSERT INTO mailbox_items (kind, fingerprint, source, title, summary, proposal, evidence, confidence, effect, urgent) "
        "VALUES ('schedule_ok', $1, 'admiral', $2, $3, $4::jsonb, $5::jsonb, 1, $6, true) RETURNING id",
        f"job:{job['id']}:{uuid.uuid4().hex[:8]}", f"It's time: {job['title']}"[:200],
        f"{spec.get('tool')}{' · ' + what if what else ''}"[:600],
        json.dumps({"job_id": job["id"], "tool": spec.get("tool"), "args": spec.get("args") or {}}),
        json.dumps([f"You scheduled this for {when.say(_planned(job).astimezone(ZoneInfo(job['tz'])))}", reason]),
        "I run it now, exactly as shown. After a few minutes it's too late, and I won't.")
    await db.pool().execute("UPDATE jobs SET mailbox_id=$2 WHERE id=$1", job["id"], item)
    await mailbox.notify(app.state.redis)
    return item


async def approve(app, job_id: int, ok: bool) -> str:
    """His answer on a scheduled change-tier job's card. Too late (past its grace) means it doesn't run. One
    guarded update each way, so a cancel or the sweep in between always wins."""
    from . import morpho
    cur = await db.pool().fetchrow("SELECT * FROM jobs WHERE id=$1", job_id)
    if cur is not None and recurring(dict(cur)) and cur["status"] == "waiting" and not ok:
        sp = _j(cur["spec"])                             # an automation: no means not this time
        out = await skip(app, job_id, "you said no", only=("waiting",))
        await morpho.record(app, sp.get("tool") or "", sp.get("args") or {}, cur["user_id"], False, "schedule")
        return f"Skipped this time. {out['skipped']}"
    if not ok:
        row = await db.pool().fetchrow("UPDATE jobs SET status='cancelled', last_outcome='skipped', updated_at=now() "
                                       "WHERE id=$1 AND status='waiting' RETURNING *", job_id)
        if row is None:
            raise ValueError("That isn't waiting for you anymore.")
        sp = _j(row["spec"])
        await morpho.record(app, sp.get("tool") or "", sp.get("args") or {}, row["user_id"], False, "schedule")
        await _log(job_id, _planned(dict(row)), "skipped", None, "you said no")
        return "Skipped. It won't run."
    row = await db.pool().fetchrow(
        "UPDATE jobs SET spec = spec || jsonb_build_object('approved', extract(epoch FROM COALESCE(planned_at, due_at))), "
        "status='scheduled', phase='run', next_at=now(), "
        "updated_at=now() WHERE id=$1 AND status='waiting' AND (grace_seconds IS NULL OR "
        "now() <= COALESCE(planned_at, due_at) + make_interval(secs => grace_seconds)) RETURNING id", job_id)
    if row is not None:
        j = await db.pool().fetchrow("SELECT spec, user_id FROM jobs WHERE id=$1", job_id)
        sp = _j(j["spec"])
        await morpho.record(app, sp.get("tool") or "", sp.get("args") or {}, j["user_id"], True, "schedule")
        poke()
        return "Approved. Running it now."
    late = await db.pool().fetchrow(
        "UPDATE jobs SET status='missed', last_outcome='missed', updated_at=now() WHERE id=$1 AND status='waiting' "
        "RETURNING *", job_id)
    if late is None:
        raise ValueError("That isn't waiting for you anymore.")
    job = dict(late)
    await _log(job_id, _planned(job), "missed", None, "your OK came after it was too late")
    if recurring(job):                                   # an automation carries on from its next time
        await _carry_on(app, job)
    if job["mailbox_id"]:
        await db.pool().execute("UPDATE mailbox_items SET status='expired', updated_at=now() WHERE id=$1 "
                                "AND status IN ('waiting','deferred')", job["mailbox_id"])
    raise ValueError(f"That was due {when.say(_planned(job).astimezone(ZoneInfo(job['tz'])))}; "
                     "it's too late to run it now.")


async def _carry_on(app, job: dict) -> None:
    """An automation whose run was missed (no OK in time) goes on to its next time."""
    status, planned, nxt, phase = slot(app, job, max(_planned(job), now()))
    await db.pool().execute(
        "UPDATE jobs SET status=$2, planned_at=$3, due_at=$3, next_at=$4, phase=$5, task_id=NULL, tries=0, mailbox_id=NULL, "
        "spec = spec - 'approved', updated_at=now() WHERE id=$1 AND status='missed'", job["id"], status, planned, nxt, phase)
    poke()


async def _started(task_id: Any) -> bool:
    return bool(await db.pool().fetchval("SELECT 1 FROM task_runs WHERE id=$1", uuid.UUID(str(task_id))))


async def _launch(app, job: dict, body) -> str:
    """Start this occurrence's task under an id saved on the job first: after a crash, the job finds the task it
    started (and waits for it) instead of starting another."""
    from .tasks import launch
    task_id = str(job["task_id"]) if job.get("task_id") else None
    if task_id and await _started(task_id):
        return task_id                                   # it had started before a restart: wait for that one
    task_id = task_id or str(uuid.uuid4())
    await _mine("UPDATE jobs SET task_id=$2, tries=tries+1 WHERE id=$1", job["id"], uuid.UUID(task_id))
    job["task_id"] = task_id
    try:
        return await launch(app, body, task_id=task_id)
    except Exception as exc:
        if await _started(task_id):                      # it got as far as the function, which may be doing it
            raise Dispatched(str(getattr(exc, "detail", exc)))
        await _mine("UPDATE jobs SET task_id=NULL WHERE id=$1", job["id"])     # it never started: no task to wait for
        job["task_id"] = None
        raise


class Dispatched(Exception):
    """The task was sent but the function never confirmed it got it: it may still happen, so no retry."""


async def _run_tool(app, job: dict) -> None:
    from fastapi import HTTPException as HTTPExc
    from . import admiral
    from .tasks import StartTask, wait_for_result
    spec = _j(job["spec"])
    try:
        task_id = await _launch(app, job, StartTask(tool=spec["tool"], args=spec.get("args") or {}, user=job["user_id"],
                                                    trigger=_trigger(job), confirmed=_approved(job, spec)))
    except Dispatched as exc:
        await _close_heads_up(app, job["id"])
        await _finish(app, job, "failed", f"{exc} (not retried: it may still have happened)")
        await _tell(app, job, "Not sure it happened", f"{job['title']}: the function didn't confirm it got it, and I "
                                                      "didn't try again so it can't happen twice. Check it."[:250])
        return
    except HTTPExc as exc:
        d = exc.detail if isinstance(exc.detail, dict) else {}
        await _close_heads_up(app, job["id"])
        if d.get("needs_confirmation"):
            await _finish(app, job, "waiting", "waiting for your OK")
            if await db.pool().fetchval("SELECT status FROM jobs WHERE id=$1", job["id"]) != "waiting":
                return                                   # deleted while it ran: nothing to ask
            await _card(app, job, str(d.get("reason") or "it needs your OK"))
            await _tell(app, job, f"It's time: {job['title']}", "It needs your OK first: it's in your mailbox.")
            return
        await _retry_or_fail(app, job, str(d.get("reason") or exc.detail))
        return
    await _close_heads_up(app, job["id"])
    res = await wait_for_result(task_id, STEP_WAIT)
    if res is None:
        await _finish(app, job, "failed", f"still running after {STEP_WAIT // 60} minutes", task_id)
        await _tell(app, job, "That didn't finish", f"{job['title']} was still running after {STEP_WAIT // 60} minutes.")
        return
    data = res.get("result") or {}
    if res["status"] == "completed":
        why = admiral.unconfirmed(data)
        head = str(data.get("headline") or job["title"])
        if not why and isinstance(data.get("kept"), dict):      # kept for a display that isn't open: not shown yet
            await _finish(app, job, "kept", head, task_id)          # (never "done", never a failure; the display
            return                                                  # it's kept for has already told him)
        if why:
            await _finish(app, job, "unconfirmed", why, task_id)
            await _tell(app, job, "Asked, not confirmed", f"{head}. I couldn't confirm it happened: {why}"[:250])
        else:
            await _finish(app, job, "done", head, task_id)
            if not recurring(job):                       # an automation that worked is in the digest, not on his phone
                await _tell(app, job, "Done", head[:250])
        return
    await _retry_or_fail(app, job, str(res.get("error") or res["status"]), task_id)


def _trigger(job: dict) -> str:
    return "automation" if recurring(job) else "schedule"


ROUTINE_END = {"done": "done", "kept": "kept", "gaps": "unconfirmed", "failed": "failed", "stopped": "failed",
               "expired": "failed"}


async def _run_routine(app, job: dict) -> None:
    """One of his routines (a skill made from his words, Morpho's): its plan runs, each step governed and checked
    from its own result, and the run is what the plan ends as. A step that needs his OK waits on the plan's panel,
    but only as long as the job's grace: an OK after that is too late, so the plan is stopped and the run missed.
    A routine is never started twice: once its plan exists, that plan is followed (after a restart too)."""
    from . import admiral
    from .tasks import StartTask, wait_for_result
    spec = _j(job["spec"])
    try:
        task_id = await _launch(app, job, StartTask(tool="morpho.run", args={"skill": str(spec["skill_id"])},
                                                    user=job["user_id"], trigger=_trigger(job)))
    except Dispatched as exc:
        task_id = str(job["task_id"])
        if not await _plan_of(task_id):
            await _close_heads_up(app, job["id"])
            await _finish(app, job, "failed", f"{exc} (not retried: it may still have started)")
            await _tell(app, job, "Not sure it started", f"{job['title']}: the routine didn't confirm it started, and I "
                                                         "didn't try again so it can't run twice. Check it."[:250])
            return
    await _close_heads_up(app, job["id"])
    res = await wait_for_result(task_id, 60)
    plan_id = (((res or {}).get("result") or {}).get("plan") or {}).get("id") or await _plan_of(task_id)
    if not plan_id:
        if res is None:                                  # it may still start: never a second one
            await _finish(app, job, "failed", "the routine didn't answer (not retried, so it can't run twice)", task_id)
            await _tell(app, job, "Not sure it started", f"{job['title']}: the routine didn't answer in time.")
            return
        await _retry_or_fail(app, job, str(res.get("error") or "the routine didn't start"), task_id)
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STEP_WAIT
    grace_end = _planned(job) + timedelta(seconds=job["grace_seconds"]) if job["grace_seconds"] is not None else None
    status, report = "running", {}
    while True:
        row = await db.pool().fetchrow("SELECT status, report, error FROM plans WHERE id=$1", uuid.UUID(str(plan_id)))
        status = row["status"] if row else "failed"
        if status in ROUTINE_END:
            report = _j(row["report"]) if row and row["report"] else {"headline": (row or {}).get("error")}
            break
        if status == "waiting":                          # a step waits for his OK: only until the grace runs out
            if grace_end is not None and now() > grace_end:
                if not await admiral.stop_plan(app, str(plan_id), "Your OK came after it was too late, so I stopped it."):
                    continue                             # it moved on just now (his answer won): look again
                due = when.say(_planned(job).astimezone(ZoneInfo(job["tz"])))
                await _finish(app, job, "missed", f"a step waited for your OK past its time (due {due}); stopped", task_id)
                await _tell(app, job, "Missed", f"{job['title']} was due {due}; a step needed your OK and it didn't come "
                                                "in time, so I stopped it.")
                return
            deadline = loop.time() + STEP_WAIT            # once he answers, the rest gets a full window
        elif loop.time() > deadline:
            await _finish(app, job, "failed", f"still running after {STEP_WAIT // 60} minutes", task_id)
            await _tell(app, job, "That didn't finish", f"{job['title']} was still running after {STEP_WAIT // 60} minutes.")
            return
        await asyncio.sleep(1)
    head = str((report or {}).get("headline") or job["title"])[:250]
    outcome = ROUTINE_END[status]
    gaps = "; ".join((report or {}).get("gaps") or (report or {}).get("kept") or [])
    await _finish(app, job, outcome, (gaps or head)[:300], task_id)
    # A plan that failed or ended with gaps has already told him (its own notification); one that worked is in
    # the digest when it's an automation's.
    if outcome == "done" and not recurring(job):
        await _tell(app, job, "Done", head)


async def _plan_of(task_id: Any) -> str | None:
    pid = await db.pool().fetchval("SELECT id FROM plans WHERE task_id=$1 ORDER BY created_at LIMIT 1",
                                   uuid.UUID(str(task_id)))
    return str(pid) if pid else None


async def _retry_or_fail(app, job: dict, error: str, task_id: Any = None) -> None:
    tries = await db.pool().fetchval("SELECT tries FROM jobs WHERE id=$1", job["id"]) or 0
    r = rules(app)
    wait = float(r["retry_after_seconds"])
    hs = int(r["heads_up_seconds"]) if job.get("heads_up") else 0
    within = job["grace_seconds"] is None or (now() + timedelta(seconds=wait + hs) - _planned(job)).total_seconds() \
        <= job["grace_seconds"]
    if tries < 2 and within:                             # once more, a little later (a computer coming back)
        await _mine("UPDATE jobs SET status='scheduled', next_at=now() + make_interval(secs => $2), due_at=now() + "
                    "make_interval(secs => $2 + $3), phase=$4, task_id=NULL, tries=GREATEST(tries, 1), lease_until=NULL, "
                    "updated_at=now() WHERE id=$1", job["id"], wait, float(hs), "heads_up" if hs else "run")
        await _log(job["id"], _planned(job), "failed", task_id, f"{error} (trying again)", job.get("_started"))
        return
    await _finish(app, job, "failed", error, task_id)
    await _tell(app, job, "That didn't work", f"{job['title']}: {error}"[:250])


async def _run_plan(app, job: dict) -> None:
    from .tasks import StartTask, wait_for_result
    spec = _j(job["spec"])
    task_id = await _launch(app, job, StartTask(tool="admiral.plan", args={"goal": spec["goal"]}, user=job["user_id"],
                                                trigger="schedule"))
    res = await wait_for_result(task_id, 120)
    if res and res["status"] == "completed":
        await _finish(app, job, "done", "the plan started; its panel shows it", task_id)
    else:
        err = (res or {}).get("error") or "it didn't start"
        await _finish(app, job, "failed", err, task_id)
        await _tell(app, job, "A scheduled plan didn't start", f"{spec['goal'][:100]}: {err}"[:250])


# ---- the plan sweep: waiting plans, stale cards ------------------------------------------------------------

@system("admiral.sweep", every=lambda app: 5)
async def sweep(app, spec: dict) -> dict | None:
    """Plans waiting on him: a reminder after a while, expired after longer. A change-tier job's card that
    went unanswered past its grace: too late, so it's taken back."""
    from . import admiral, mailbox, push
    r = rules(app)
    for p in await db.pool().fetch(
            "UPDATE plans SET reminded=true WHERE status='waiting' AND NOT reminded AND waiting_since < now() - "
            "make_interval(secs => $1) RETURNING id, goal, device", float(r["remind_waiting_hours"]) * 3600):
        try:
            await push.notify(app, "A plan is still waiting for you", p["goal"][:150], f"/view?plan={p['id']}",
                              f"plan-{str(p['id'])[:8]}",
                              devices=[p["device"]] if p["device"] is not None else None)
        except Exception:
            log.exception("plan reminder")
    expired = await db.pool().fetch(
        "UPDATE plans SET status='expired', error=$2, updated_at=now() WHERE status='waiting' AND waiting_since < now() - "
        "make_interval(secs => $1) RETURNING id", float(r["expire_waiting_days"]) * 86400,
        f"No answer in {r['expire_waiting_days']} days, so I stopped it.")
    changed = False
    for p in expired:
        changed |= bool(await db.pool().fetch(
            "UPDATE mailbox_items SET status='expired', updated_at=now() WHERE status IN ('waiting','deferred') AND id IN "
            "(SELECT mailbox_id FROM plan_steps WHERE plan_id=$1 AND status='waiting') RETURNING id", p["id"]))
        await db.pool().execute("UPDATE plan_steps SET status='skipped', note='no answer in time' "
                                "WHERE plan_id=$1 AND status IN ('pending','waiting')", p["id"])
        await admiral.publish(app, str(p["id"]))
    for j in await db.pool().fetch(
            "SELECT id, COALESCE(planned_at, due_at) AS due_at, mailbox_id FROM jobs WHERE status='waiting' "
            "AND grace_seconds IS NOT NULL AND COALESCE(planned_at, due_at) < now() - make_interval(secs => grace_seconds)"):
        gone = await db.pool().fetchrow("UPDATE jobs SET status='missed', last_outcome='missed', updated_at=now() "
                                        "WHERE id=$1 AND status='waiting' RETURNING *", j["id"])
        if gone is None:
            continue
        await _log(j["id"], j["due_at"], "missed", None, "no OK in time")
        if recurring(dict(gone)):
            await _carry_on(app, dict(gone))
        if j["mailbox_id"]:
            changed |= bool(await db.pool().fetchval(
                "UPDATE mailbox_items SET status='expired', updated_at=now() WHERE id=$1 AND status IN ('waiting','deferred') "
                "RETURNING id", j["mailbox_id"]))
    for j in await db.pool().fetch(                      # cut off between "missed" and its next time (a restart)
            "SELECT * FROM jobs WHERE status='missed' AND created_by='owner' AND NOT (rule ? 'once') "
            "AND updated_at < now() - interval '1 minute'"):
        await _carry_on(app, dict(j))
    if changed:
        await mailbox.notify(app.state.redis)
    return None


@system("admiral.resume")
async def resume_plan(app, spec: dict) -> dict | None:
    """A plan step's retry is due (it waited a few minutes): the plan carries on."""
    from . import admiral
    admiral.start(app, str(spec["plan_id"]))
    return None


# ---- his tools: schedule, scheduled, unschedule (Admiral's) -------------------------------------------------

_REMIND = __import__("re").compile(r"^(?:please\s+)?remind\s+me\s+(?P<rest>.+)$", __import__("re").I)
_LEAD = __import__("re").compile(r"^(?:to|that|about|of)\s+", __import__("re").I)
NOT_SCHEDULED = ("admiral.schedule", "admiral.scheduled", "admiral.unschedule", "admiral.stop", "admiral.plans")
# Never scheduled: her own planning, Pulsar's (an automation is saved only by his confirmation of its read-back),
# and Morpho's except running a skill.
NEVER_SCHEDULED = ("admiral.", "pulsar.", "morpho.")


def schedulable(tool: str) -> bool:
    return not tool.startswith(NEVER_SCHEDULED) or tool == "morpho.run"


def _once_patterns(app) -> list:
    return [p for p in app.state.config.current.fastpath.patterns
            if p.tool not in NOT_SCHEDULED and (schedulable(p.tool) or p.tool == "admiral.plan")]


def _sentence(s: str) -> str:
    s = " ".join(str(s or "").split()).strip(" .,")
    return s[:1].upper() + s[1:]


async def _resolve(app, what: str) -> tuple[str, dict, str]:
    """His words for a command -> exactly what will run, settled now (the same fast path as saying it now)."""
    from . import fastpath, registry
    if when.mentions_repeat(what):
        raise ValueError("That repeats, so it's an automation, not something for once. Say it again the same way and "
                         "I'll read it back as one.")
    m = fastpath.match(what, _once_patterns(app))
    if m is None:
        raise ValueError(f"I can't tell exactly which command \"{what}\" is, so I didn't schedule it. Say it the way "
                         f"you'd ask me right now (like \"lock the front door\"), or ask me to remind you instead.")
    tool = m.pattern.tool
    resolved = await registry.resolve(app.state.redis, tool)
    if resolved is None:
        raise ValueError(f"{tool} isn't available right now, so I can't schedule it.")
    return tool, fastpath.coerce(m.args, resolved.tool.input_schema or {}), _sentence(what)


def fast_ok(app, say_: str) -> bool:
    """Whether a "later" fast path really is one: a time read by code, and a reminder or a command I know.
    Otherwise the request goes to the model (never to another fast path, which could run it now)."""
    from . import fastpath
    try:
        m = _REMIND.match(say_)
        _, rest, _ = when.split(m.group("rest") if m else say_, now(), tz_name(app))
    except when.Unclear:
        return False
    if m or _REMIND.match(rest):
        text = _LEAD.sub("", rest).strip()
        return bool(text) and not when.repeat_first(text)       # "remind me at 5 every day to…" repeats
    if when.mentions_repeat(rest):
        return False                                            # what's left still repeats: the model hears it
    return fastpath.match(rest, _once_patterns(app)) is not None


async def schedule_tool(app, user: str, args: dict) -> dict:
    from . import admiral, registry
    tz, n = tz_name(app), now()
    say_, what = str(args.get("say") or "").strip(), str(args.get("what") or "").strip()
    reminder, goal = str(args.get("reminder") or "").strip(), str(args.get("goal") or "").strip()
    tool, targs = str(args.get("tool") or "").strip(), args.get("args") if isinstance(args.get("args"), dict) else {}
    try:
        if say_:
            m = _REMIND.match(say_)
            at, rest, _ = when.split(m.group("rest") if m else say_, n, tz)
            if m:
                reminder = _LEAD.sub("", rest)
            else:
                what = rest
        elif args.get("when"):
            at = when.parse(str(args["when"]), n, tz)
        else:
            raise ValueError("When should I do it?")
    except when.Unclear as exc:
        raise ValueError(str(exc))
    if at <= n:
        raise ValueError("That time has already passed.")
    if reminder and _REMIND.match(reminder):
        reminder = _LEAD.sub("", _REMIND.match(reminder).group("rest"))
    if (reminder and when.repeat_first(reminder)) or (what and not reminder and when.mentions_repeat(what)):
        raise ValueError("That repeats, so it's an automation (pulsar.automate), not something for once.")
    if what and not (reminder or goal or tool) and _REMIND.match(what):
        reminder, what = _LEAD.sub("", _REMIND.match(what).group("rest")), ""
    task_id = str(args.get("_self") or "")
    device = await db.pool().fetchval("SELECT device FROM task_runs WHERE id=$1", uuid.UUID(task_id)) if task_id else None
    note = None
    if reminder:
        kind, title, spec = "reminder", _sentence(reminder), {"text": _sentence(reminder)}
    else:
        if what and not (goal or tool):
            tool, targs, title = await _resolve(app, what)
            if tool == "admiral.plan":
                goal, tool = str(targs.get("goal") or what), ""
        if goal:
            kind, title, spec = "plan", f"Plan: {_sentence(goal)}"[:200], {"goal": goal}
        elif tool:
            if not schedulable(tool):
                raise ValueError("I can't schedule that.")
            resolved = await registry.resolve(app.state.redis, tool)
            if resolved is None:
                raise ValueError(f"{tool} isn't available right now, so I can't schedule it.")
            known = set(((resolved.tool.input_schema or {}).get("properties") or {}).keys())
            targs = {k: v for k, v in (targs or {}).items() if not known or k in known}
            tier = admiral.tier_of(app, tool)
            if tier == "never":
                raise ValueError("That's never allowed, so I won't schedule it.")
            if tier == "change":
                note = "When it's time, I'll ask for your OK first (in your mailbox and on your phone)."
            kind, spec = "tool", {"tool": tool, "args": targs}
            title = title if what else _sentence(f"{tool.split('.', 1)[1].replace('_', ' ')}")
        else:
            raise ValueError("What should I do then?")
    job = await add(app, kind, title, spec, at, user, device)
    v = view(job, n)
    if job["heads_up"]:
        hs = int(rules(app)["heads_up_seconds"])
        note = (note + " " if note else "") + f"You'll get a heads-up {hs // 60 or hs} {'minute' if hs >= 60 else 'seconds'} " \
               "before, with a Cancel."
    return {"headline": f"{'Reminder' if kind == 'reminder' else 'Scheduled'} {v['when']}: {title}",
            "scheduled": {"job": v, **({"note": note} if note else {})}}


async def scheduled_tool(app, user: str, args: dict) -> dict:
    n = now()
    up = await db.pool().fetch(
        "SELECT * FROM jobs WHERE created_by='owner' AND user_id=$1 AND status IN ('scheduled','running','waiting') "
        "ORDER BY due_at LIMIT 30", user)
    recent = await db.pool().fetch(
        "SELECT r.*, j.title, j.tz FROM job_runs r JOIN jobs j ON j.id = r.job_id WHERE j.created_by='owner' "
        "AND j.user_id=$1 ORDER BY r.started_at DESC LIMIT 10", user)
    mine = await db.pool().fetch(
        "SELECT * FROM jobs WHERE created_by='system' AND status IN ('scheduled','running') ORDER BY name, due_at LIMIT 30")
    z = ZoneInfo(tz_name(app))
    items = [view(dict(r), n) for r in up]
    for i, r in zip(items, up):
        if r["status"] == "waiting":
            i["note"] = "waiting for your OK (in your mailbox)"
    runs = [{"title": r["title"], "when": when.say(r["due_at"].astimezone(z), n), "outcome": r["outcome"],
             **({"late": f"{r['late_seconds'] // 60} min late"} if r["late_seconds"] >= 120 else {}),
             **({"note": r["note"]} if r["note"] else {})} for r in recent]
    own = []
    for r in mine:
        rule = _j(r["rule"])
        own.append({"title": r["title"], "next": when.say(r["due_at"].astimezone(z), n),
                    **({"every": f"every {rule['every_minutes']:g} min"} if rule.get("every_minutes") else {}),
                    **({"last": r["last_outcome"]} if r["last_outcome"] else {})})
    head = f"{len(items)} scheduled" + (f" · next: {items[0]['title']}, {items[0]['when']}" if items else "") \
        if items else "Nothing scheduled"
    return {"headline": head, "schedule": {"upcoming": items, "recent": runs, "mine": own}}


def _hay(job: dict) -> str:
    due = (job.get("planned_at") or job["due_at"]).astimezone(ZoneInfo(job["tz"]))
    h12 = due.strftime("%I").lstrip("0")
    ap = due.strftime("%p").lower()
    clock = [f"{h12}{ap}", h12, due.strftime("%H:%M"), f"{h12}:{due.strftime('%M')}{ap}", due.strftime("%A").lower(),
             "tomorrow" if due.date() == (now().astimezone(due.tzinfo).date() + timedelta(days=1)) else "today"]
    kind = {"reminder": "reminder", "plan": "plan", "tool": "action", "routine": "routine"}.get(job["kind"], "")
    extra = ["automation", (repeats(job) or "").lower(), str(_j(job.get("spec")).get("name") or "").lower()] \
        if recurring(job) else []
    return " ".join([job["title"].lower(), kind, *clock, *extra])


async def unschedule_tool(app, user: str, args: dict) -> dict:
    import re
    up = [dict(r) for r in await db.pool().fetch(
        "SELECT * FROM jobs WHERE created_by='owner' AND user_id=$1 AND status IN ('scheduled','waiting') ORDER BY due_at", user)]
    if not up:
        raise ValueError("Nothing is scheduled.")
    if args.get("job"):
        hits = [j for j in up if str(j["id"]) == str(args["job"]).strip()]
    else:
        stop = {"the", "my", "a", "an", "one", "that", "this", "job", "scheduled", "to", "for", "at", "of", "on", "timer"}
        words = [w for w in re.findall(r"[a-z0-9:]+", when._clean(str(args.get("which") or ""))) if w not in stop]
        words = ["reminder" if w == "reminders" else w for w in words]
        once = [j for j in up if not recurring(j)]
        hits = [j for j in once if all(w in _hay(j) for w in words)] if words else (once if len(once) == 1 else [])
        if not hits and words and any(all(w in _hay(j) for w in words) for j in up if recurring(j)):
            raise ValueError("That's one of your automations. Say \"skip the next one\" to skip a single run, or "
                             "\"delete the … automation\" to stop it for good.")
    if not hits:
        raise ValueError("Nothing scheduled matches that. Say \"what's scheduled\" to see them.")
    if len(hits) > 1:
        n = now()
        raise ValueError("Which one? " + "; ".join(f"{j['title']} ({view(j, n)['when']})" for j in hits[:5]))
    if recurring(hits[0]):
        raise ValueError("That's one of your automations: \"skip\" or \"delete\" it instead.")
    job = await cancel(app, hits[0]["id"], "you cancelled it")
    return {"headline": f"Cancelled: {job['title']}", "did": {"node": "Admiral", "what": f"Cancelled: {job['title']}"}}
