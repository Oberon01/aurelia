"""Admiral's scheduling engine end to end: reminders, commands later with a heads-up and Cancel, a change-tier
command asking him when it's due (and too late is too late), missed runs, never twice across two runners,
resuming a started task after a restart instead of re-running it, her own follow-ups, and the fast path.
Real Postgres and Redis; the commands' function is a stand-in."""
import asyncio
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
import pytest
import redis as redis_sync

from .conftest import DB, REDIS, free_port

pytestmark = pytest.mark.skipif(not (DB and REDIS), reason="needs TEST_DATABASE_URL and TEST_REDIS_URL")


def sql(query, *args):
    async def go():
        conn = await asyncpg.connect(DB)
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()
    return asyncio.run(go())


@pytest.fixture(scope="module")
def enginefn(stack):
    """act (confirmed, or not with unconfirmed), look, confirm (change tier). Remembers each run."""
    import uvicorn
    from fastapi import FastAPI
    orch, port = stack["orch"], free_port()
    fn, seen = FastAPI(), {"runs": []}

    @fn.post("/tools/{tool}/invoke", status_code=202)
    async def invoke(tool: str, body: dict):
        args = body.get("args") or {}

        async def work():
            seen["runs"].append((tool, args, time.time()))
            data = {"did": {"node": "Den", "what": args.get("what") or tool,
                            **({"unconfirmed": True} if args.get("unconfirmed") else {})}, "headline": f"Did {tool}"}
            async with httpx.AsyncClient() as c:
                await c.post(f"{orch}/tasks/{body['task_id']}/result", json={"status": "completed", "data": data})
        asyncio.create_task(work())
        return {}

    srv = uvicorn.Server(uvicorn.Config(fn, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    time.sleep(0.5)

    def register():
        prop = lambda *ks: {"type": "object", "properties": {k: {"type": "string"} for k in ks}}
        httpx.post(f"{orch}/registry/functions", json={"name": "enginefn", "description": "test",
                                                       "url": f"http://127.0.0.1:{port}", "tools": [
            {"name": "act", "description": "act", "input_schema": prop("what", "unconfirmed")},
            {"name": "look", "description": "look", "input_schema": prop("what")},
            {"name": "confirm", "description": "change", "input_schema": prop("what")}]}).raise_for_status()
    seen["register"] = register
    yield seen
    srv.should_exit = True


@pytest.fixture
def fn(stack, enginefn):
    enginefn["register"]()
    return enginefn


def task(orch, tool, args, wait=15):
    tid = httpx.post(f"{orch}/tasks", json={"tool": tool, "args": args}).json()["task_id"]
    deadline = time.time() + wait
    while time.time() < deadline:
        t = httpx.get(f"{orch}/tasks/{tid}").json()
        if t["status"] != "running":
            return t, (json.loads(t["result"]) if isinstance(t["result"], str) else t["result"])
        time.sleep(0.1)
    raise AssertionError(f"{tool} never finished")


def job(job_id):
    [row] = sql("SELECT * FROM jobs WHERE id=$1", int(job_id))
    return dict(row)


def runs(job_id):
    return [dict(r) for r in sql("SELECT * FROM job_runs WHERE job_id=$1 ORDER BY id", int(job_id))]


def until_job(job_id, statuses, wait=20):
    deadline = time.time() + wait
    while time.time() < deadline:
        j = job(job_id)
        if j["status"] in statuses:
            return j
        time.sleep(0.2)
    raise AssertionError(f"job stayed {j['status']} ({j['last_outcome']}): {runs(job_id)}")


def schedule(orch, **args):
    t, res = task(orch, "admiral.schedule", args)
    assert t["status"] == "completed", t["error"]
    return res


def test_a_reminder_arrives_on_time(stack, fn):
    res = schedule(stack["orch"], say="remind me in 2 seconds to stretch")
    j = res["scheduled"]["job"]
    assert res["headline"].startswith("Reminder today at") and res["headline"].endswith(": Stretch")
    assert j["kind"] == "reminder" and j["in"] == "now"
    done = until_job(j["id"], {"done"})
    [r] = runs(j["id"])
    assert r["outcome"] == "done" and r["late_seconds"] < 5 and done["runs"] == 1


def test_a_command_later_gets_a_heads_up_runs_and_is_checked_by_its_result(stack, fn):
    r = redis_sync.Redis.from_url(REDIS)
    sub = r.pubsub()
    sub.subscribe("aurelia:events")
    time.sleep(0.1)
    res = schedule(stack["orch"], when="in 4 seconds", tool="enginefn.act", args={"what": "lock it", "bogus": 1})
    j = res["scheduled"]["job"]
    assert j["args"] == {"what": "lock it"} and j["heads_up"] and "heads-up" in res["scheduled"]["note"]
    row = job(j["id"])
    assert row["phase"] == "heads_up" and (row["due_at"] - row["next_at"]).total_seconds() == pytest.approx(2, abs=0.5)
    done = until_job(j["id"], {"done", "failed"})
    assert done["last_outcome"] == "done"
    events, deadline = [], time.time() + 2
    while time.time() < deadline:
        m = sub.get_message(timeout=0.2)
        if m and m["type"] == "message":
            events.append(json.loads(m["data"]))
    heads = [e for e in events if e["tool"] == "admiral.heads_up" and e["type"] == "panel.result"]
    assert heads and heads[0]["data"]["heads_up"]["job"] == j["id"]
    run = [x for x in fn["runs"] if x[1].get("what") == "lock it"]
    assert len(run) == 1 and run[0][2] >= datetime.fromisoformat(heads[0]["data"]["heads_up"]["at"]).timestamp() - 0.5
    [rr] = runs(j["id"])
    [t] = sql("SELECT trigger, tool FROM task_runs WHERE id=$1", rr["task_id"])
    assert (t["trigger"], t["tool"]) == ("schedule", "enginefn.act")


def test_cancel_during_the_heads_up_and_it_never_runs(stack, fn):
    orch = stack["orch"]
    res = schedule(orch, when="in 5 seconds", tool="enginefn.act", args={"what": "never run"})
    jid = res["scheduled"]["job"]["id"]
    deadline = time.time() + 6
    while job(jid)["phase"] != "run" and time.time() < deadline:            # the heads-up went out
        time.sleep(0.1)
    r = httpx.post(f"{orch}/admiral/jobs/{jid}/cancel")
    assert r.status_code == 200 and r.json()["done"].startswith("Cancelled")
    time.sleep(4)
    assert job(jid)["status"] == "cancelled" and [x["outcome"] for x in runs(jid)] == ["cancelled"]
    assert not [x for x in fn["runs"] if x[1].get("what") == "never run"]
    assert httpx.post(f"{orch}/admiral/jobs/{jid}/cancel").status_code == 409


def test_an_unconfirmed_result_is_not_called_done(stack, fn):
    res = schedule(stack["orch"], when="in 1 second", tool="enginefn.look", args={"what": "x", "unconfirmed": "y"})
    j = until_job(res["scheduled"]["job"]["id"], {"done", "failed"})
    assert j["last_outcome"] == "done"                                       # look takes no 'unconfirmed' arg: dropped
    res = schedule(stack["orch"], when="in 1 second", tool="enginefn.act", args={"what": "y", "unconfirmed": "yes"})
    j = until_job(res["scheduled"]["job"]["id"], {"done", "failed"}, wait=25)
    assert j["last_outcome"] == "unconfirmed" and runs(j["id"])[-1]["note"] == "y"


def test_a_change_tier_command_asks_him_when_its_due_and_too_late_is_too_late(stack, fn):
    orch = stack["orch"]
    res = schedule(orch, when="in 1 second", tool="enginefn.confirm", args={"what": "unlock"})
    assert "ask for your OK" in res["scheduled"]["note"]
    jid = res["scheduled"]["job"]["id"]
    j = until_job(jid, {"waiting"})
    [card] = sql("SELECT * FROM mailbox_items WHERE id=$1", j["mailbox_id"])
    assert card["kind"] == "schedule_ok" and json.loads(card["proposal"])["args"] == {"what": "unlock"}
    assert not [x for x in fn["runs"] if x[1].get("what") == "unlock"]
    r = httpx.post(f"{orch}/mailbox/{card['id']}/decide", json={"action": "modify", "changes": {"args": {"what": "x"}}})
    assert r.status_code == 200
    j = until_job(jid, {"done", "failed"})
    [t] = sql("SELECT confirmed, args FROM task_runs WHERE id=$1", runs(jid)[-1]["task_id"])
    assert t["confirmed"] is True and json.loads(t["args"]) == {"what": "unlock"}    # what was scheduled, not the card

    res = schedule(orch, when="in 1 second", tool="enginefn.confirm", args={"what": "too late"})
    jid = res["scheduled"]["job"]["id"]
    j = until_job(jid, {"waiting"})
    sql("UPDATE jobs SET planned_at = now() - interval '1 hour' WHERE id=$1", jid)   # he answers an hour late
    r = httpx.post(f"{orch}/mailbox/{j['mailbox_id']}/decide", json={"action": "approve"})
    assert r.status_code == 409 and "too late" in r.json()["detail"]
    assert job(jid)["status"] == "missed"
    assert sql("SELECT status FROM mailbox_items WHERE id=$1", j["mailbox_id"])[0]["status"] == "expired"
    assert not [x for x in fn["runs"] if x[1].get("what") == "too late"]


def test_a_missed_run_is_marked_missed_never_run_late(stack, fn):
    late = datetime.now(timezone.utc) - timedelta(minutes=30)
    [row] = sql("INSERT INTO jobs (kind, title, spec, rule, due_at, next_at, grace_seconds) VALUES ('tool', 'Late one', "
                "$1::jsonb, '{\"once\": true}'::jsonb, $2, $2, 60) RETURNING id",
                json.dumps({"tool": "enginefn.act", "args": {"what": "missed one"}}), late)
    j = until_job(row["id"], {"missed"})
    [r] = runs(j["id"])
    assert r["outcome"] == "missed" and r["late_seconds"] >= 1700
    assert not [x for x in fn["runs"] if x[1].get("what") == "missed one"]


def _route(path, fn_):
    from app.main import app
    if not any(getattr(r, "path", "") == path for r in app.routes):
        app.add_api_route(path, fn_, methods=["POST"])


def test_two_runners_never_run_a_job_twice(stack, fn):
    from app import engine
    from app.main import app
    counts = {}

    async def count(app_, spec):
        counts[spec["n"]] = counts.get(spec["n"], 0) + 1
        await asyncio.sleep(0.05)
        return None
    engine.system("test.count")(count)

    async def second():                          # another orchestrator's runner, in the same database
        asyncio.get_running_loop().create_task(engine.loop(app))
        return {}
    _route("/_test/engine/second", second)
    httpx.post(f"{stack['orch']}/_test/engine/second").raise_for_status()
    now = datetime.now(timezone.utc)
    for n in range(40):
        sql("INSERT INTO jobs (kind, name, title, created_by, spec, rule, due_at, next_at) VALUES ('system', 'test.count', "
            "'count', 'system', $1::jsonb, '{\"once\": true}'::jsonb, $2, $2)", json.dumps({"n": n}), now)
    deadline = time.time() + 15
    while len(counts) < 40 and time.time() < deadline:
        time.sleep(0.2)
    assert sorted(counts) == list(range(40)) and set(counts.values()) == {1}


def test_after_a_restart_a_started_task_is_waited_for_not_run_again(stack, fn):
    t, _ = task(stack["orch"], "enginefn.act", {"what": "before the restart"})
    before = len([x for x in fn["runs"] if x[1].get("what") == "before the restart"])
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    [row] = sql("INSERT INTO jobs (kind, title, spec, rule, due_at, next_at, status, task_id, lease_until, claimed_by, tries) "
                "VALUES ('tool', 'Mid-run', $1::jsonb, '{\"once\": true}'::jsonb, $2, $2, 'running', $3, $2, 'gone', 1) "
                "RETURNING id", json.dumps({"tool": "enginefn.act", "args": {"what": "before the restart"}}), past,
                uuid.UUID(t["id"]))
    j = until_job(row["id"], {"done", "failed"})
    assert j["last_outcome"] == "done" and str(runs(row["id"])[-1]["task_id"]) == t["id"]
    assert len([x for x in fn["runs"] if x[1].get("what") == "before the restart"]) == before


def test_her_own_follow_ups_go_again_until_done(stack, fn):
    from app import engine
    seen = []

    async def step(app_, spec):
        seen.append(spec["i"])
        return {"again_in": 0.3, "spec": {"i": spec["i"] + 1}} if spec["i"] < 2 else None
    engine.system("test.steps")(step)

    async def start():
        from app.main import app
        return {"id": await engine.after(app, 0.1, "test.steps", {"i": 0}, "steps")}
    _route("/_test/engine/after", start)
    jid = httpx.post(f"{stack['orch']}/_test/engine/after").json()["id"]
    until_job(jid, {"done"})
    assert seen == [0, 1, 2] and [r["outcome"] for r in runs(jid)] == ["done"] * 3


def test_his_own_recurring_jobs_are_on_the_engine_once(stack, fn):
    rows = sql("SELECT name, rule FROM jobs WHERE created_by='system' AND status IN ('scheduled','running') "
               "AND rule ? 'every_minutes' ORDER BY name")
    names = [r["name"] for r in rows]
    assert {"admiral.sweep", "memory.maintenance", "mnemosyne.record"} <= set(names)
    assert len(names) == len(set(names))
    t, res = task(stack["orch"], "admiral.scheduled", {})
    mine = {m["title"] for m in res["schedule"]["mine"]}
    assert {"memory.maintenance", "mnemosyne.record", "admiral.sweep"} <= mine


def test_he_cancels_by_his_words_and_is_asked_which_when_its_unclear(stack, fn):
    orch = stack["orch"]
    sql("UPDATE jobs SET status='cancelled' WHERE created_by='owner' AND status IN ('scheduled','waiting')")
    a = schedule(orch, say="remind me tomorrow at 9 pm to water the plants")["scheduled"]["job"]
    b = schedule(orch, say="remind me tomorrow at 11 pm to take the trash out")["scheduled"]["job"]
    assert a["when"] == "tomorrow at 9:00 PM" and b["title"] == "Take the trash out"
    t, _ = task(orch, "admiral.unschedule", {"which": "reminder"})
    assert t["status"] == "failed" and t["error"].startswith("Which one?") and "Water the plants" in t["error"]
    t, res = task(orch, "admiral.unschedule", {"which": "11 pm one"})
    assert t["status"] == "completed" and res["headline"] == "Cancelled: Take the trash out"
    assert job(b["id"])["status"] == "cancelled" and job(a["id"])["status"] == "scheduled"
    t, res = task(orch, "admiral.scheduled", {})
    assert [u["title"] for u in res["schedule"]["upcoming"]] == ["Water the plants"]
    assert res["schedule"]["recent"][0]["outcome"] == "cancelled"
    t, _ = task(orch, "admiral.unschedule", {"which": "the plants"})
    assert t["status"] == "completed"


def test_scheduling_refuses_what_it_cant_read_or_settle(stack, fn):
    orch = stack["orch"]
    for args, why in (({"say": "remind me sometime to call"}, "couldn't find when"),
                      ({"when": "tomorrow", "reminder": "x"}, "At what time"),
                      ({"when": "in 5 minutes", "tool": "admiral.plan", "args": {"goal": "x"}}, "can't schedule"),
                      ({"when": "in 5 minutes", "tool": "nosuch.tool"}, "isn't available"),
                      ({"when": "in 5 minutes", "what": "do the thing"}, "can't tell exactly which command")):
        t, _ = task(orch, "admiral.schedule", args)
        assert t["status"] == "failed" and why in t["error"], (args, t["error"])


def test_the_fast_path_schedules_and_never_runs_it_now(stack, fn):
    from app.config import FastPathPattern
    from app.main import app
    import yaml
    from pathlib import Path
    real = yaml.safe_load(Path(__file__).parents[4].joinpath("config", "aurelia.yaml").read_text())["fastpath"]["patterns"]
    mine = [FastPathPattern(**p) for p in real if p["name"].startswith("schedule-")]
    mine.append(FastPathPattern(name="act", match=r"^flip (?P<what>.+)$", tool="enginefn.act", args={"what": "{what}"}))
    cfg = app.state.config.current.fastpath
    old = cfg.patterns
    cfg.patterns = mine
    try:
        out = httpx.post(f"{stack['orch']}/ask", json={"text": "flip the switch in 2 seconds"}, timeout=30).json()
        assert out["route"] == "fastpath"
        [t] = sql("SELECT id, tool, result FROM task_runs WHERE id = ANY($1::uuid[])", [uuid.UUID(x) for x in out["task_ids"]])
        assert t["tool"] == "admiral.schedule"
        deadline = time.time() + 10
        while not [x for x in fn["runs"] if x[1].get("what") == "the switch"] and time.time() < deadline:
            time.sleep(0.2)
        hit = [x for x in fn["runs"] if x[1].get("what") == "the switch"]
        assert len(hit) == 1
        res = json.loads(t["result"]) if t["result"] else {}
        if res:
            assert res["scheduled"]["job"]["tool"] == "enginefn.act"
        # not a command I know: it isn't scheduled (or run): the model hears it
        out = httpx.post(f"{stack['orch']}/ask", json={"text": "tomorrow I want to rest"}, timeout=30).json()
        assert out["route"] != "fastpath"
    finally:
        cfg.patterns = old


def test_the_sweep_reminds_then_expires_a_waiting_plan(stack, fn):
    from app import engine
    from app.main import app

    async def sweep():
        await engine.sweep(app, {})
        return {}
    _route("/_test/engine/sweep", sweep)
    pid = uuid.uuid4()
    sql("INSERT INTO plans (id, goal, status, waiting_since) VALUES ($1, 'wait for me', 'waiting', now() - interval '5 hours')", pid)
    sql("INSERT INTO plan_steps (plan_id, n, title, tool, args, status) VALUES ($1, 1, 'Q', 'admiral.ask', "
        "'{\"question\": \"?\"}'::jsonb, 'waiting')", pid)
    httpx.post(f"{stack['orch']}/_test/engine/sweep").raise_for_status()
    [p] = sql("SELECT status, reminded FROM plans WHERE id=$1", pid)
    assert p["status"] == "waiting" and p["reminded"] is True
    sql("UPDATE plans SET waiting_since = now() - interval '4 days' WHERE id=$1", pid)
    httpx.post(f"{stack['orch']}/_test/engine/sweep").raise_for_status()
    [p] = sql("SELECT status, error FROM plans WHERE id=$1", pid)
    assert p["status"] == "expired" and "No answer in 3 days" in p["error"]
    assert sql("SELECT status FROM plan_steps WHERE plan_id=$1", pid)[0]["status"] == "skipped"


def test_a_late_heads_up_job_is_missed_not_run_hours_late(stack, fn):
    late = datetime.now(timezone.utc) - timedelta(hours=3)
    [row] = sql("INSERT INTO jobs (kind, title, spec, rule, planned_at, due_at, next_at, phase, heads_up, grace_seconds) "
                "VALUES ('tool', 'Lock it late', $1::jsonb, '{\"once\": true}'::jsonb, $2, $2, $2, 'heads_up', true, 60) "
                "RETURNING id", json.dumps({"tool": "enginefn.act", "args": {"what": "hours late"}}), late)
    j = until_job(row["id"], {"missed"})
    assert j["phase"] == "heads_up"                                  # no heads-up went out for a run that won't happen
    time.sleep(3)
    assert not [x for x in fn["runs"] if x[1].get("what") == "hours late"]


def test_a_task_id_saved_before_a_crash_is_started_once(stack, fn):
    tid = uuid.uuid4()                                              # saved on the job; the task never got created
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    [row] = sql("INSERT INTO jobs (kind, title, spec, rule, planned_at, due_at, next_at, status, task_id, lease_until, "
                "claimed_by, tries, grace_seconds) VALUES ('tool', 'Crashed before start', $1::jsonb, '{\"once\": true}'::jsonb, "
                "$2, $2, $2, 'running', $3, $2, 'gone', 1, 600) RETURNING id",
                json.dumps({"tool": "enginefn.act", "args": {"what": "start me once"}}), past, tid)
    j = until_job(row["id"], {"done", "failed"})
    assert j["last_outcome"] == "done" and str(runs(row["id"])[-1]["task_id"]) == str(tid)   # under the saved id
    assert len([x for x in fn["runs"] if x[1].get("what") == "start me once"]) == 1


def test_an_ok_after_he_cancelled_never_brings_it_back(stack, fn):
    orch = stack["orch"]
    res = schedule(orch, when="in 1 second", tool="enginefn.confirm", args={"what": "cancelled first"})
    jid = res["scheduled"]["job"]["id"]
    j = until_job(jid, {"waiting"})
    assert httpx.post(f"{orch}/admiral/jobs/{jid}/cancel").status_code == 200
    assert sql("SELECT status FROM mailbox_items WHERE id=$1", j["mailbox_id"])[0]["status"] == "expired"
    from app import engine
    from app.main import app

    async def approve_late():
        try:
            await engine.approve(app, jid, True)
        except ValueError as exc:
            return {"error": str(exc)}
        return {"error": None}
    _route("/_test/engine/approve", approve_late)
    out = httpx.post(f"{orch}/_test/engine/approve").json()
    assert out["error"] == "That isn't waiting for you anymore."
    time.sleep(2)
    assert job(jid)["status"] == "cancelled" and not [x for x in fn["runs"] if x[1].get("what") == "cancelled first"]


def test_a_task_the_function_never_confirmed_getting_is_not_retried(stack, fn):
    orch = stack["orch"]
    dead = f"http://127.0.0.1:{free_port()}"                    # nothing listens: it can't say it got it
    httpx.post(f"{orch}/registry/functions", json={"name": "enginefn", "description": "test", "url": dead, "tools": [
        {"name": "look", "description": "look", "input_schema": {"type": "object", "properties": {"what": {"type": "string"}}}}]}
               ).raise_for_status()
    try:
        res = schedule(orch, when="in 1 second", tool="enginefn.look", args={"what": "unconfirmed delivery"})
        j = until_job(res["scheduled"]["job"]["id"], {"failed", "done"})
        assert j["status"] == "failed" and j["tries"] == 1
        [r] = runs(j["id"])
        assert "not retried" in r["note"]
    finally:
        fn["register"]()
