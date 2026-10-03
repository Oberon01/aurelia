"""Admiral: planning and long tasks (Phase 9, slice 1).

A big request ("find the best computer within a $1000 budget") becomes a plan of steps, each one a
call to one of Aurelia's own tools, run through the same task path as everything else (governance,
rate limits, the log). The plan and its steps are kept in Postgres, so a plan survives a restart and
picks up where it was.

How a plan runs:
  - Steps whose dependencies are done run together (at most PARALLEL at once).
  - A step whose arguments depend on earlier results ("fill") gets them from the model, given those
    results, just before it runs.
  - A step counts as done only from its own result: completed, and nothing in it saying it couldn't
    be confirmed ("unconfirmed": true, "verified": false). An unconfirmed step is not done; nothing
    that depends on it runs, and the plan ends saying so. Never "done" because a command was sent.
  - A step that needs his OK (governance: change tier) waits; the plan's panel asks him, and his
    answer there resumes it. A failed step is tried once more, then the plan stops at it.
  - When every step is done, the model checks the results against what he asked and writes the
    report: what was found, and anything it didn't cover. That's the plan's answer.

The plan's panel is the admiral.plan task's own panel, updated as the plan moves; his phone hears
when a plan finishes while he isn't looking. Asking questions through the mailbox, replanning,
the scheduling engine and Morpho (plans kept as skills) are the next slices.
"""
import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import auth, db, registry

log = logging.getLogger("aurelia.admiral")
router = APIRouter(prefix="/admiral", tags=["admiral"])
NAME = "admiral"
MAX_STEPS = 12
PARALLEL = 3
STEP_WAIT = 600                 # how long one step may run before the plan stops waiting for it
TRIES = 2
RESULT_KEEP = 12000             # characters of a step's result kept (and shown to the model)
TERMINAL = {"done", "unconfirmed", "failed", "skipped"}
_running: dict[str, asyncio.Task] = {}

TOOLS = [
    registry.ToolSpec(
        name="plan", description="For a request that needs several steps across your tools (research and compare, "
                                 "find then act, gather from several places): make a plan of steps and run it. Each "
                                 "step is one of your tools, checked from its own result; the plan ends in a report "
                                 "checked against what he asked. goal: what he asked, in full. Its panel shows the "
                                 "steps as they run; tell him the plan has started and what it will do, not results.",
        input_schema={"type": "object", "properties": {"goal": {"type": "string"}}, "required": ["goal"]},
        cancellable=False, display="panel"),
    registry.ToolSpec(
        name="plans", description="His recent plans: each one's goal, status and how far it got.",
        input_schema={"type": "object", "properties": {}}, cancellable=False, freshness_seconds=10, display="panel"),
    registry.ToolSpec(
        name="stop", description="Stop a running plan (the newest one if he doesn't say which). Steps already "
                                 "running finish; nothing else starts.",
        input_schema={"type": "object", "properties": {"plan": {"type": "string"}}}, cancellable=False),
]


# ---- the model's parts: planning, filling arguments, the final check ----------------------------------

PLAN_SYSTEM = """You plan multi-step work for Aurelia, a personal assistant. You get his request and the tools
Aurelia has. Reply with JSON only, no prose, in exactly this shape:
{"steps": [{"n": 1, "title": "short, what this step does", "tool": "function.tool", "args": {...},
            "depends": [], "fill": false}],
 "report": "what the final report must cover"}
Rules:
- 1 to %d steps. Each step is exactly one tool from the list, with arguments matching its schema.
- "depends": numbers of earlier steps whose results it needs. Steps without shared dependencies run in parallel.
- "fill": true when its arguments can only be known from those results (e.g. read the pages a search found);
  then give "args" as your best partial guess, and they're completed just before it runs.
- Prefer looking things up (search, read, research) to acting. Don't plan steps that buy, pay, send messages,
  post, delete, install or close anything unless he asked for exactly that; those always need his OK anyway.
- Never plan another plan. Don't invent tools or arguments."""

FILL_SYSTEM = """Complete the arguments for one step of a plan, using the results of the steps it depends on.
Reply with the JSON object of arguments only, matching the tool's schema."""

CHECK_SYSTEM = """You check a finished plan against what he asked and write its report. Reply with JSON only:
{"headline": "one line: the answer", "summary": "the answer in full, plain text, a few short paragraphs or a
list; cite where facts came from", "meets_goal": true/false, "gaps": ["what the results don't cover"]}
Use only what the steps' results say. If a step wasn't confirmed or failed, say what that leaves open."""


def _json_in(text: str) -> Any:
    """The JSON a model replied with (allowing a stray sentence or ``` fences around it)."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            raise ValueError("the model didn't reply with JSON")
        return json.loads(m.group(0))


async def _model(app, plan_id: str, system: str, user_text: str, purpose: str = "reason") -> str:
    from . import assistant
    from .llm.base import Message
    cfg = app.state.config.current.llm
    if assistant._uses_cloud(cfg) and await assistant._cloud_tokens_this_month() >= cfg.monthly_token_cap:
        raise ValueError("I've reached this month's model budget, so I can't plan or check steps until it resets.")
    started = time.monotonic()
    completion = await app.state.llm.complete(purpose, system, [Message("user", user_text)], [])
    await assistant._log_llm_call(plan_id, completion, started)     # counted toward the month, under the plan
    return completion.text


def tier_of(app, qualified: str) -> str:
    fn, _, tool = qualified.partition(".")
    pol = app.state.governance.functions.get(fn)
    if pol and tool in pol.tools:
        return pol.tools[tool].tier
    return app.state.governance.charter.enforced.default_tier


async def catalog(app) -> dict[str, dict]:
    """Every tool a plan may use (not Admiral's own), with its schema and governance tier."""
    out = {}
    for fn in await registry.list_all(app.state.redis):
        if fn.name == NAME:
            continue
        for t in fn.tools:
            q = f"{fn.name}.{t.name}"
            out[q] = {"description": t.description, "schema": t.input_schema or {}, "tier": tier_of(app, q)}
    return out


def _catalog_text(cat: dict[str, dict]) -> str:
    lines = []
    for q, t in sorted(cat.items()):
        props = (t["schema"].get("properties") or {})
        req = set(t["schema"].get("required") or [])
        args = ", ".join(f"{k}{'' if k in req else '?'}: {v.get('type', 'any')}" for k, v in props.items())
        lines.append(f"- {q} ({t['tier']}) ({args}): {t['description'][:300]}")
    return "\n".join(lines)


def validate(raw: Any, cat: dict[str, dict]) -> list[dict]:
    """The model's plan, checked: real tools, known arguments, dependencies only on earlier steps."""
    steps = raw.get("steps") if isinstance(raw, dict) else None
    if not isinstance(steps, list) or not steps:
        raise ValueError("the plan has no steps")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"the plan has {len(steps)} steps; at most {MAX_STEPS}")
    out = []
    for i, s in enumerate(steps, 1):
        tool = str((s or {}).get("tool") or "")
        if tool not in cat:
            raise ValueError(f"step {i} uses {tool or 'no tool'}, which I don't have")
        args = s.get("args") if isinstance(s.get("args"), dict) else {}
        known = set((cat[tool]["schema"].get("properties") or {}).keys())
        args = {k: v for k, v in args.items() if not known or k in known}
        deps = sorted({int(d) for d in (s.get("depends") or []) if str(d).isdigit() and 0 < int(d) < i})
        out.append({"n": i, "title": str(s.get("title") or tool)[:120], "tool": tool, "args": args, "depends": deps,
                    "fill": bool(s.get("fill")) and bool(deps)})
    return out


# ---- the plan store --------------------------------------------------------------------------------

async def load(plan_id: str) -> tuple[dict, list[dict]]:
    p = await db.pool().fetchrow("SELECT * FROM plans WHERE id=$1", uuid.UUID(plan_id))
    if p is None:
        raise ValueError("No such plan.")
    rows = await db.pool().fetch("SELECT * FROM plan_steps WHERE plan_id=$1 ORDER BY n", uuid.UUID(plan_id))
    steps = []
    for r in rows:
        d = dict(r)
        for k in ("args", "result"):
            if isinstance(d[k], str):
                d[k] = json.loads(d[k])
        steps.append(d)
    plan = dict(p)
    if isinstance(plan.get("report"), str):
        plan["report"] = json.loads(plan["report"])
    return plan, steps


async def _set_plan(plan_id: str, **kw) -> None:
    sets = ", ".join(f"{k}=${i}" + ("::jsonb" if k == "report" else "") for i, k in enumerate(kw, 2))
    vals = [json.dumps(v) if k == "report" else v for k, v in kw.items()]
    await db.pool().execute(f"UPDATE plans SET {sets}, updated_at=now() WHERE id=$1", uuid.UUID(plan_id), *vals)


async def _set_step(plan_id: str, n: int, **kw) -> None:
    def cast(k):
        return "::jsonb" if k in ("args", "result") else ""
    sets = ", ".join(f"{k}=${i}{cast(k)}" for i, k in enumerate(kw, 3))
    vals = [json.dumps(v) if k in ("args", "result") else v for k, v in kw.items()]
    await db.pool().execute(f"UPDATE plan_steps SET {sets} WHERE plan_id=$1 AND n=$2", uuid.UUID(plan_id), n, *vals)


# ---- what the panel shows ----------------------------------------------------------------------------

def view(plan: dict, steps: list[dict]) -> dict:
    done = sum(1 for s in steps if s["status"] == "done")
    st = plan["status"]
    head = {"planning": "Planning", "running": f"Plan · {done} of {len(steps)} steps",
            "waiting": "Plan · waiting for you", "done": "Plan done", "gaps": "Plan done, with gaps",
            "failed": "Plan stopped", "stopped": "Plan stopped"}.get(st, "Plan")
    if plan.get("report") and plan["report"].get("headline"):
        head = str(plan["report"]["headline"])[:160]
    return {"headline": head, "plan": {
        "id": str(plan["id"]), "goal": plan["goal"], "status": st, "error": plan.get("error"),
        "steps": [{"n": s["n"], "title": s["title"], "tool": s["tool"], "status": s["status"],
                   **({"note": s["note"]} if s.get("note") else {}),
                   # what he's being asked to OK, exactly as it would run
                   **({"args": s["args"]} if s["status"] == "waiting" else {})} for s in steps],
        **({"report": plan["report"]} if plan.get("report") else {})}}


async def publish(app, plan_id: str) -> None:
    from .events import PanelEvent, publish as pub
    plan, steps = await load(plan_id)
    if not plan.get("task_id"):
        return
    await pub(app.state.redis, PanelEvent(type="panel.result", task_id=str(plan["task_id"]), tool=f"{NAME}.plan",
                                          device=plan.get("device"),
                                          data={"status": "completed", "partial": False, **view(plan, steps)}))


# ---- checking a step's own result -------------------------------------------------------------------

def unconfirmed(result: Any) -> str | None:
    """Why a completed result doesn't count as done: it says itself that it couldn't be confirmed."""
    found = []

    def walk(v, depth=0):
        if depth > 6 or found:
            return
        if isinstance(v, dict):
            if v.get("confirmed") == [] and (v.get("later") or v.get("failed")):     # a send no display showed
                found.append("no display has shown it yet")
                return
            if v.get("unconfirmed") is True or v.get("verified") is False or v.get("confirmed") is False:
                if v.get("kind") and v.get("node"):          # Constellation's open: what the computer couldn't see
                    found.append(f"{v['node']} couldn't see {v.get('what') or 'it'} start")
                else:
                    found.append(str(v.get("what") or v.get("message") or v.get("note") or "it couldn't be confirmed"))
                return
            for x in v.values():
                walk(x, depth + 1)
        elif isinstance(v, list):
            for x in v[:50]:
                walk(x, depth + 1)
    walk(result)
    return found[0][:300] if found else None


def _trim(v: Any) -> Any:
    s = json.dumps(v, default=str)
    if len(s) <= RESULT_KEEP:
        return v
    return {"truncated": True, "text": s[:RESULT_KEEP]}


# ---- running ---------------------------------------------------------------------------------------

async def _fill(app, plan: dict, step: dict, steps: list[dict]) -> dict:
    cat = await catalog(app)
    deps = {s["n"]: s for s in steps if s["n"] in step["depends"]}
    text = (f"Goal: {plan['goal']}\n\nThis step: {step['title']}\nTool: {step['tool']}\nSchema: "
            f"{json.dumps(cat.get(step['tool'], {}).get('schema', {}))}\nYour guess so far: {json.dumps(step['args'])}\n\n"
            + "\n\n".join(f"Step {n} ({d['title']}) result:\n{json.dumps(d.get('result'), default=str)[:RESULT_KEEP // 2]}"
                          for n, d in deps.items()))
    got = _json_in(await _model(app, str(plan["id"]), FILL_SYSTEM, text, "chat"))
    if not isinstance(got, dict):
        raise ValueError("the model didn't give arguments")
    known = set((cat.get(step["tool"], {}).get("schema", {}).get("properties") or {}).keys())
    return {k: v for k, v in got.items() if not known or k in known}


async def _run_step(app, plan: dict, step: dict, steps: list[dict], confirmed: bool = False) -> None:
    from fastapi import HTTPException as HTTPExc
    from .tasks import StartTask, launch, wait_for_result
    pid, n = str(plan["id"]), step["n"]
    args = step["args"]
    try:
        if step["fill"] and step["attempts"] == 0 and not confirmed:   # an approved step runs as he saw it
            args = await _fill(app, plan, step, steps)
            await _set_step(pid, n, args=args)
        await _set_step(pid, n, status="running", started_at=_now(), attempts=step["attempts"] + 1, note=None)
        task_id = await launch(app, StartTask(tool=step["tool"], args=args, user=plan["user_id"], trigger="admiral",
                                              confirmed=confirmed, display="brief", device=plan.get("device")))
    except HTTPExc as exc:
        d = exc.detail if isinstance(exc.detail, dict) else {}
        if d.get("needs_confirmation"):
            await _set_step(pid, n, status="waiting", note=str(d.get("reason") or "needs your OK"),
                            attempts=step["attempts"])          # asking him isn't a try
            return
        await _set_step(pid, n, status="failed", note=str(d.get("reason") or exc.detail), finished_at=_now())
        return
    except ValueError as exc:
        await _set_step(pid, n, status="failed", note=str(exc)[:300], finished_at=_now())
        return
    await _set_step(pid, n, task_id=uuid.UUID(task_id))
    await publish(app, pid)
    await _await_step(app, plan, n, task_id)


async def _await_step(app, plan: dict, n: int, task_id: str) -> None:
    from .tasks import wait_for_result
    pid = str(plan["id"])
    res = await wait_for_result(task_id, STEP_WAIT)
    if res is None:
        await _set_step(pid, n, status="failed", note=f"it was still running after {STEP_WAIT // 60} minutes",
                        finished_at=_now())
        return
    if res["status"] == "completed":
        why = unconfirmed(res["result"])
        await _set_step(pid, n, status="unconfirmed" if why else "done", result=_trim(res["result"]), note=why,
                        finished_at=_now())
        return
    _, steps = await load(pid)
    step = next(s for s in steps if s["n"] == n)
    if step["attempts"] < TRIES:                            # once more, the same way
        await _set_step(pid, n, status="pending", note=f"tried again: {str(res.get('error') or '')[:200]}")
        return
    await _set_step(pid, n, status="failed", note=str(res.get("error") or res["status"])[:300], finished_at=_now())


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def ready(steps: list[dict]) -> list[dict]:
    done = {s["n"] for s in steps if s["status"] == "done"}
    return [s for s in steps if s["status"] == "pending" and all(d in done for d in s["depends"])]


def blocked(steps: list[dict]) -> list[dict]:
    """Pending steps that can never run: something they need failed, isn't confirmed, or was skipped."""
    bad = {s["n"] for s in steps if s["status"] in ("failed", "unconfirmed", "skipped")}
    out, changed = [], True
    while changed:
        changed = False
        for s in steps:
            if s["status"] == "pending" and s not in out and any(d in bad for d in s["depends"]):
                out.append(s)
                bad.add(s["n"])
                changed = True
    return out


async def run(app, plan_id: str) -> None:
    """Run a plan to its end (or until it waits for him). Safe to call again after a restart."""
    try:
        while True:
            plan, steps = await load(plan_id)
            if plan["status"] not in ("running",):
                return
            for s in blocked(steps):
                await _set_step(plan_id, s["n"], status="skipped", note="something it needs isn't done")
            plan, steps = await load(plan_id)
            for s in [s for s in steps if s["status"] == "running"]:          # after a restart
                if s.get("task_id"):
                    await _await_step(app, plan, s["n"], str(s["task_id"]))
                else:                                   # it never started: start it again
                    await _set_step(plan_id, s["n"], status="pending", attempts=max(0, s["attempts"] - 1))
            plan, steps = await load(plan_id)
            go = ready(steps)[:PARALLEL]
            if go:
                await asyncio.gather(*(_run_step(app, plan, s, steps) for s in go))
                await publish(app, plan_id)
                continue
            if any(s["status"] == "waiting" for s in steps):
                await _set_plan(plan_id, status="waiting")
                await publish(app, plan_id)
                await _tell(app, plan, "A plan needs your OK", f"{plan['goal'][:80]}: a step is waiting for you.")
                return
            if any(s["status"] in ("pending", "running") for s in steps):
                await asyncio.sleep(1)
                continue
            await finish(app, plan_id)
            return
    except Exception as exc:
        log.exception("plan %s", plan_id)
        await _set_plan(plan_id, status="failed", error=f"Something went wrong running it: {exc}"[:400])
        await publish(app, plan_id)
    finally:
        _running.pop(plan_id, None)


async def finish(app, plan_id: str) -> None:
    plan, steps = await load(plan_id)
    ok = [s for s in steps if s["status"] == "done"]
    if not ok:
        bad = next((s for s in steps if s["status"] in ("failed", "unconfirmed")), None)
        await _set_plan(plan_id, status="failed",
                        error=(f"Step {bad['n']} ({bad['title']}) {bad['status']}: {bad.get('note') or ''}" if bad
                               else "No step finished.")[:400])
        await publish(app, plan_id)
        await _tell(app, plan, "A plan stopped", plan["goal"][:120])
        return
    text = f"What he asked: {plan['goal']}\n\n" + "\n\n".join(
        f"Step {s['n']}: {s['title']} ({s['tool']}) — {s['status']}"
        + (f" ({s['note']})" if s.get("note") else "")
        + (f"\nResult: {json.dumps(s.get('result'), default=str)[:RESULT_KEEP // 3]}" if s["status"] == "done" else "")
        for s in steps)
    try:
        rep = _json_in(await _model(app, plan_id, CHECK_SYSTEM, text))
    except ValueError as exc:
        await _set_plan(plan_id, status="failed", error=f"The steps ran, but I couldn't check them: {exc}"[:400])
        await publish(app, plan_id)
        return
    report = {"headline": str(rep.get("headline") or "")[:200], "summary": str(rep.get("summary") or "")[:6000],
              "meets_goal": bool(rep.get("meets_goal")),
              "gaps": [str(g)[:300] for g in (rep.get("gaps") or []) if g][:8]}
    partial = any(s["status"] != "done" for s in steps)
    status = "done" if report["meets_goal"] and not partial else "gaps"
    if partial and not report["gaps"]:
        report["gaps"] = [f"Step {s['n']} ({s['title']}) {s['status']}" for s in steps if s["status"] != "done"]
    await _set_plan(plan_id, status=status, report=report)
    await publish(app, plan_id)
    await _tell(app, plan, "Plan done" if status == "done" else "Plan done, with gaps", report["headline"] or plan["goal"][:120])


async def _tell(app, plan: dict, title: str, body: str) -> None:
    if plan.get("device") is None:
        return
    try:
        from . import push
        await push.notify(app, title, body, "/view", f"plan-{str(plan['id'])[:8]}", devices=[plan["device"]])
    except Exception:
        log.exception("plan notification")


def start(app, plan_id: str) -> None:
    if plan_id not in _running or _running[plan_id].done():
        _running[plan_id] = asyncio.create_task(run(app, plan_id))


async def resume_all(app) -> None:
    """After a restart: every plan that was running carries on."""
    for r in await db.pool().fetch("SELECT id FROM plans WHERE status='running'"):
        start(app, str(r["id"]))


# ---- the tools -------------------------------------------------------------------------------------

async def plan_tool(app, user: str, args: dict) -> dict:
    goal = " ".join(str(args.get("goal") or "").split())[:2000]
    if not goal:
        raise ValueError("Plan what?")
    task_id = str(args.get("_self") or "")
    device = await db.pool().fetchval("SELECT device FROM task_runs WHERE id=$1", uuid.UUID(task_id)) if task_id else None
    plan_id = str(uuid.uuid4())
    await db.pool().execute("INSERT INTO plans (id, user_id, goal, device, task_id) VALUES ($1, $2, $3, $4, $5)",
                            uuid.UUID(plan_id), user, goal, device, uuid.UUID(task_id) if task_id else None)
    cat = await catalog(app)
    try:
        raw = _json_in(await _model(app, plan_id, PLAN_SYSTEM % MAX_STEPS,
                                    f"His request: {goal}\n\nTools:\n{_catalog_text(cat)}"))
        steps = validate(raw, cat)
    except ValueError as exc:
        await _set_plan(plan_id, status="failed", error=f"I couldn't make a plan: {exc}"[:400])
        raise ValueError(f"I couldn't make a plan for that: {exc}")
    for s in steps:
        await db.pool().execute(
            "INSERT INTO plan_steps (plan_id, n, title, tool, args, depends, fill) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)",
            uuid.UUID(plan_id), s["n"], s["title"], s["tool"], json.dumps(s["args"]), s["depends"], s["fill"])
    await _set_plan(plan_id, status="running")
    plan, rows = await load(plan_id)
    start(app, plan_id)
    out = view(plan, rows)
    out["headline"] = f"Plan · {len(rows)} steps"
    return out


async def plans_tool(app, user: str, args: dict) -> dict:
    rows = await db.pool().fetch(
        "SELECT p.id, p.goal, p.status, p.report, p.updated_at, count(s.*) AS steps, "
        "count(s.*) FILTER (WHERE s.status='done') AS done FROM plans p LEFT JOIN plan_steps s ON s.plan_id=p.id "
        "WHERE p.user_id=$1 GROUP BY p.id ORDER BY p.updated_at DESC LIMIT 15", user)
    items = [{"id": str(r["id"]), "goal": r["goal"], "status": r["status"], "steps": r["steps"], "done": r["done"]}
             for r in rows]
    return {"headline": f"{len(items)} recent plan{'s' if len(items) != 1 else ''}", "plans": {"items": items}}


async def stop_tool(app, user: str, args: dict) -> dict:
    pid = str(args.get("plan") or "")
    row = await db.pool().fetchrow(
        "SELECT id, goal FROM plans WHERE user_id=$1 AND status IN ('running','waiting','planning')"
        + (" AND id::text LIKE $2" if pid else "") + " ORDER BY updated_at DESC LIMIT 1",
        *([user, pid + "%"] if pid else [user]))
    if row is None:
        raise ValueError("No plan is running.")
    await _set_plan(str(row["id"]), status="stopped", error="Stopped at your word.")
    await db.pool().execute("UPDATE plan_steps SET status='skipped', note='the plan was stopped' "
                            "WHERE plan_id=$1 AND status IN ('pending','waiting')", row["id"])
    await publish(app, str(row["id"]))
    return {"headline": "Plan stopped", "did": {"node": "Admiral", "what": f"Stopped: {row['goal'][:120]}"}}


HANDLERS = {"plan": plan_tool, "plans": plans_tool, "stop": stop_tool}


# ---- his answer to a step that waits for him --------------------------------------------------------

class Decide(BaseModel):
    approve: bool


@router.post("/plans/{plan_id}/steps/{n}/decide")
async def decide(plan_id: str, n: int, body: Decide, request: Request):
    dev = await auth.asker(request.headers)
    if dev is not None and dev["kind"] == "screen":
        raise HTTPException(403, "A TV's screen can't decide that.")
    try:
        plan, steps = await load(plan_id)
    except (ValueError, Exception):
        raise HTTPException(404, "No such plan.")
    step = next((s for s in steps if s["n"] == n), None)
    if step is None or step["status"] != "waiting":
        raise HTTPException(409, "That step isn't waiting for you.")
    if not body.approve:
        await _set_step(plan_id, n, status="skipped", note="you said no", finished_at=_now())
        await _set_plan(plan_id, status="running")
        start(request.app, plan_id)
        return {"ok": True, "step": "skipped"}
    await _set_plan(plan_id, status="running")
    await _set_step(plan_id, n, status="pending")
    step["status"] = "pending"

    async def go():
        await _run_step(request.app, {**plan, "status": "running"}, step, steps, confirmed=True)
        start(request.app, plan_id)
    asyncio.create_task(go())
    return {"ok": True, "step": "approved"}


# ---- registration and the function contract (like Quasar) ------------------------------------------

async def register_loop(app) -> None:
    from .settings import settings
    await resume_all(app)
    while True:
        ttl = app.state.config.current.registry.function_ttl_seconds
        try:
            await registry.register(app.state.redis, registry.FunctionRegistration(
                name=NAME, description="Planning: a request that needs several steps becomes a checked plan.",
                url=f"{settings.self_url.rstrip('/')}/admiral", tools=TOOLS), ttl)
        except Exception:
            log.exception("couldn't register Admiral; will retry")
        await asyncio.sleep(max(3, ttl // 3))


@router.get("/health")
async def health():
    return {"status": "ok"}


class Invoke(BaseModel):
    task_id: str
    args: dict = {}


@router.post("/tools/{tool}/invoke", status_code=202)
async def invoke(tool: str, body: Invoke, request: Request):
    if tool not in HANDLERS:
        raise HTTPException(404, f"admiral has no tool '{tool}'")
    asyncio.create_task(_run(request.app, tool, body.task_id, body.args))
    return {}


@router.post("/cancel/{task_id}", status_code=202)
async def cancel(task_id: str):
    return {}


async def _run(app, tool: str, task_id: str, args: dict) -> None:
    from . import tasks
    try:
        row = await db.pool().fetchrow("SELECT user_id FROM task_runs WHERE id=$1", uuid.UUID(task_id))
        data = await HANDLERS[tool](app, row["user_id"] if row else "owner", {**args, "_self": task_id})
        result = tasks.Result(status="completed", data=data)
    except ValueError as exc:
        result = tasks.Result(status="failed", error=str(exc))
    except Exception as exc:
        log.exception("admiral.%s failed", tool)
        result = tasks.Result(status="failed", error=f"Something went wrong: {exc}")
    await tasks._finish(tasks._AppContext(app), task_id, f"{NAME}.{tool}", result)
    plan = (result.data or {}).get("plan") if tool == "plan" else None
    if plan and plan.get("id"):                 # the runner may have moved on already: show where it is now
        try:
            await publish(app, plan["id"])
        except Exception:
            log.exception("plan panel")
