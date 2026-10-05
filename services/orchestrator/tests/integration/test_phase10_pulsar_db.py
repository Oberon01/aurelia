"""Pulsar end to end: an automation read back and saved only on his OK, runs that repeat (checked, quiet when they
work, paused after failures in a row), skip and cancel meaning "this time", a change-tier automation asking each time,
routines made from his words and run by their name or on a schedule, an automation that waits for a computer to come
online, what's worth telling him by severity (quiet hours, repeats, "got it", cleared), the home check, and
Parallax's habits becoming automations. Real Postgres and Redis; the commands' function is a stand-in."""
import asyncio
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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


def _route(path, fn_):
    from app.main import app
    if not any(getattr(r, "path", "") == path for r in app.routes):
        app.add_api_route(path, fn_, methods=["POST"])


@pytest.fixture(scope="module")
def pfn(stack):
    """act, look, confirm (change tier) and a home 'status' (as Orbit). Remembers each run; act can be told
    to come back unconfirmed."""
    import uvicorn
    from fastapi import FastAPI
    from app.main import app
    from app.governance import ToolPolicy
    orch, port = stack["orch"], free_port()
    fn, seen = FastAPI(), {"runs": [], "unconfirmed": False,
                           "devices": [{"name": "Front Door", "kind": "lock", "locked": True, "battery": 80},
                                       {"name": "Sink Leak", "kind": "leak_sensor", "active": False}]}

    @fn.post("/tools/{tool}/invoke", status_code=202)
    async def invoke(tool: str, body: dict):
        args = body.get("args") or {}

        async def work():
            seen["runs"].append((tool, args, time.time()))
            if tool == "status":
                data = {"devices": seen["devices"]}
            else:
                data = {"did": {"node": "Den", "what": args.get("what") or tool,
                                **({"unconfirmed": True} if seen["unconfirmed"] and tool == "act" else {})},
                        "headline": f"Did {tool} {args.get('what', '')}".strip()}
            async with httpx.AsyncClient() as c:
                await c.post(f"{orch}/tasks/{body['task_id']}/result", json={"status": "completed", "data": data})
        asyncio.create_task(work())
        return {}

    srv = uvicorn.Server(uvicorn.Config(fn, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    time.sleep(0.5)
    gov = Path(app.state.governance.root) / "functions"
    (gov / "pulsarfn.yaml").write_text("function: pulsarfn\nrate_limit_per_minute: 1000\ntools:\n  act: {tier: act}\n"
                                       "  look: {tier: reference}\n  confirm: {tier: change}\n")
    app.state.governance.load_all()
    app.state.governance.functions["orbit"].tools["status"] = ToolPolicy(tier="reference")
    heads = app.state.governance.functions["admiral"].rules["schedule"]["heads_up_for"]
    if "pulsarfn.act" not in heads:
        heads.append("pulsarfn.act")

    def register():
        prop = lambda *ks: {"type": "object", "properties": {k: {"type": "string"} for k in ks}}
        for name, tools in (("pulsarfn", [{"name": "act", "description": "act", "input_schema": prop("what")},
                                          {"name": "look", "description": "look", "input_schema": prop("what")},
                                          {"name": "confirm", "description": "change", "input_schema": prop("what")}]),
                            ("orbit", [{"name": "status", "description": "home", "input_schema": prop()}])):
            httpx.post(f"{orch}/registry/functions", json={"name": name, "description": "test",
                                                           "url": f"http://127.0.0.1:{port}", "tools": tools}).raise_for_status()
    seen["register"] = register
    yield seen
    srv.should_exit = True


@pytest.fixture
def fn(stack, pfn):
    pfn["register"]()
    pfn["unconfirmed"] = False
    from app.main import app
    from app.config import FastPathPattern
    import yaml
    real = yaml.safe_load(Path(__file__).parents[4].joinpath("config", "aurelia.yaml").read_text())["fastpath"]["patterns"]
    mine = [FastPathPattern(**p) for p in real if p["tool"].startswith(("pulsar.", "admiral.schedule"))
            or p["name"] in ("routine-run",)]
    mine += [FastPathPattern(name="t-act", match=r"^flip (?P<what>.+)$", tool="pulsarfn.act", args={"what": "{what}"}),
             FastPathPattern(name="t-look", match=r"^peek (?P<what>.+)$", tool="pulsarfn.look", args={"what": "{what}"}),
             FastPathPattern(name="t-confirm", match=r"^unbolt (?P<what>.+)$", tool="pulsarfn.confirm", args={"what": "{what}"})]
    cfg = app.state.config.current.fastpath
    old = cfg.patterns
    cfg.patterns = mine
    sql("UPDATE jobs SET status='cancelled' WHERE created_by='owner' AND status IN ('scheduled','waiting','armed','paused')")
    yield pfn
    cfg.patterns = old


def ask(stack, text):
    return httpx.post(f"{stack['orch']}/ask", json={"text": text}, timeout=30).json()


def confirm(stack, out, approve=True):
    [action] = out["needs_confirmation"]
    return httpx.post(f"{stack['orch']}/ask/confirm", json={"action_id": action["action_id"], "approve": approve},
                      timeout=30).json()


def task(orch, tool, args, wait=15):
    tid = httpx.post(f"{orch}/tasks", json={"tool": tool, "args": args}).json()["task_id"]
    deadline = time.time() + wait
    while time.time() < deadline:
        t = httpx.get(f"{orch}/tasks/{tid}").json()
        if t["status"] != "running":
            return t, (json.loads(t["result"]) if isinstance(t["result"], str) else t["result"])
        time.sleep(0.1)
    raise AssertionError(f"{tool} never finished")


def jobs(**where):
    rows = [dict(r) for r in sql("SELECT * FROM jobs WHERE created_by='owner' AND NOT (rule ? 'once') "
                                 "AND status <> 'cancelled' ORDER BY id")]
    return rows


def job(job_id):
    [row] = sql("SELECT * FROM jobs WHERE id=$1", int(job_id))
    return dict(row)


def runs(job_id):
    return [dict(r) for r in sql("SELECT * FROM job_runs WHERE job_id=$1 ORDER BY id", int(job_id))]


def until(cond, wait=20, what="it"):
    deadline = time.time() + wait
    while time.time() < deadline:
        v = cond()
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError(f"{what} never happened")


def save(stack, text):
    out = ask(stack, text)
    assert out["route"] == "fastpath" and out["needs_confirmation"], out
    done = confirm(stack, out)
    [j] = [x for x in jobs() if x["status"] in ("scheduled", "armed", "running", "waiting")][-1:]
    return out, done, j


def test_an_automation_is_read_back_and_saved_only_when_he_says_yes(stack, fn):
    out = ask(stack, "flip the porch light every weekday at 7 am")
    assert out["route"] == "fastpath"
    [action] = out["needs_confirmation"]
    assert action["tool"] == "pulsar.save" and set(action["args"]) == {"draft"}
    assert out["reply"].startswith("Every weekday at 7:00 AM: Flip the porch light.")
    assert "heads-up" in out["reply"] and "First run:" in out["reply"] and out["reply"].endswith("Save it?")
    assert not jobs()                                                   # a draft only: nothing is saved yet
    assert not [r for r in fn["runs"] if r[1].get("what") == "the porch light"]
    no = confirm(stack, out, approve=False)
    assert not jobs() and "won't" in no["reply"].lower()
    out, done, j = save(stack, "flip the porch light every weekday at 7 am")
    assert done["reply"].startswith("Saved: Every weekday at 7:00 AM: Flip the porch light. First run:")   # as of saving
    rule = json.loads(j["rule"]) if isinstance(j["rule"], str) else j["rule"]
    assert rule == {"days": ["mon", "tue", "wed", "thu", "fri"], "time": "07:00"}
    assert j["kind"] == "tool" and j["heads_up"] and j["phase"] == "heads_up"
    local = j["planned_at"].astimezone(ZoneInfo("America/Chicago"))
    assert (local.hour, local.minute, local.weekday() < 5) == (7, 0, True)
    assert (j["planned_at"] - j["next_at"]).total_seconds() == pytest.approx(2, abs=0.5)    # the heads-up first
    # The same one again: refused, not doubled.
    out = ask(stack, "flip the porch light every weekday at 7 am")
    again = confirm(stack, out)
    assert "already have exactly that" in again["reply"] and len(jobs()) == 1
    # A command I don't know, or a time I can't read: the model hears it (and nothing is saved or run).
    for text in ("dance wildly every day at 7 am", "flip the porch light every day at 7"):
        out = ask(stack, text)
        assert out["route"] != "fastpath" or not out["needs_confirmation"], text
    assert len(jobs()) == 1


def test_it_repeats_checked_by_its_results_and_a_run_that_worked_stays_off_his_phone(stack, fn):
    out, done, j = save(stack, "peek the garden every 0.05 minutes")
    assert job(j["id"])["heads_up"] is False
    until(lambda: len([r for r in runs(j["id"]) if r["outcome"] == "done"]) >= 2, 25, "two runs")
    jj = job(j["id"])
    assert jj["status"] in ("scheduled", "running") and jj["runs"] >= 2 and jj["fails_in_row"] == 0
    done_runs = [r for r in runs(j["id"]) if r["outcome"] == "done"]
    [t] = sql("SELECT trigger, tool, args FROM task_runs WHERE id=$1", done_runs[0]["task_id"])
    assert (t["trigger"], t["tool"], json.loads(t["args"])) == ("automation", "pulsarfn.look", {"what": "the garden"})
    t_, res = task(stack["orch"], "pulsar.automations", {})
    [item] = [i for i in res["automations"]["items"] if i["id"] == j["id"]]
    assert item["repeats"] == "every 0.05 minutes" and item["last"][0]["outcome"] in ("done", "skipped")
    t_, res = task(stack["orch"], "pulsar.delete", {"which": "garden"})
    assert t_["status"] == "completed" and res["headline"] == "Deleted: Peek the garden"
    n = len(runs(j["id"]))
    time.sleep(4)
    assert job(j["id"])["status"] == "cancelled" and len([r for r in runs(j["id"]) if r["outcome"] == "done"]) <= n


def test_failures_in_a_row_pause_it_and_he_hears_and_resume_starts_fresh(stack, fn):
    from app.main import app
    rules = app.state.governance.functions["pulsar"].rules
    rules["severity"]["automation_paused"] = "high"
    try:
        fn["unconfirmed"] = True
        out, done, j = save(stack, "flip the fan every 0.05 minutes")
        until(lambda: job(j["id"])["status"] == "paused", 40, "the pause")
        assert job(j["id"])["fails_in_row"] >= 2
        assert [r["outcome"] for r in runs(j["id"]) if r["outcome"] != "failed"][-1] == "paused"
        [nt] = sql("SELECT * FROM notices WHERE kind='automation_paused' AND status='open' ORDER BY id DESC LIMIT 1")
        assert nt["title"] == "Paused an automation: Flip the fan" and nt["severity"] == "high"
        [card] = sql("SELECT kind, urgent, status FROM mailbox_items WHERE id=$1", nt["mailbox_id"])
        assert (card["kind"], card["urgent"], card["status"]) == ("notice", True, "waiting")
        fn["unconfirmed"] = False
        t, res = task(stack["orch"], "pulsar.resume", {"which": "the fan"})
        assert t["status"] == "completed" and "Next:" in res["did"]["what"]
        assert job(j["id"])["fails_in_row"] == 0
        until(lambda: any(r["outcome"] == "done" for r in runs(j["id"])), 25, "a good run")
        t, res = task(stack["orch"], "pulsar.pause", {"which": "fan"})
        assert t["status"] == "completed" and job(j["id"])["status"] == "paused"
        t, _ = task(stack["orch"], "pulsar.pause", {"which": "fan"})
        assert t["status"] == "failed"                                # already paused: nothing to pause
    finally:
        rules["severity"]["automation_paused"] = "low"
        fn["unconfirmed"] = False


def test_skip_and_cancel_mean_this_time_only(stack, fn):
    out, done, j = save(stack, "flip the blinds every day at 6 am")
    before = job(j["id"])["planned_at"]
    t, res = task(stack["orch"], "pulsar.skip", {"which": "blinds"})
    assert t["status"] == "completed" and "Next:" in res["did"]["what"]
    after = job(j["id"])
    assert after["status"] == "scheduled" and after["planned_at"] - before == timedelta(days=1)
    # Cancel on its heads-up (or the schedule panel) is a skip too: the automation carries on.
    r = httpx.post(f"{stack['orch']}/admiral/jobs/{j['id']}/cancel")
    assert r.status_code == 200 and r.json()["done"].startswith("Skipped this time: Flip the blinds.")
    assert job(j["id"])["planned_at"] - before == timedelta(days=2) and job(j["id"])["status"] == "scheduled"
    assert [x["outcome"] for x in runs(j["id"])] == ["skipped", "skipped"]
    # "Cancel" in words is for one-offs: an automation is skipped or deleted, never cancelled by accident.
    t, _ = task(stack["orch"], "admiral.unschedule", {"which": "blinds"})
    assert t["status"] == "failed" and "automation" in t["error"]


def test_a_change_tier_automation_asks_each_time_and_no_means_not_this_time(stack, fn):
    out, done, j = save(stack, "unbolt the gate every 0.05 minutes")
    assert "asks for your OK each time" in out["reply"]
    w = until(lambda: (lambda x: x if x["status"] == "waiting" else None)(job(j["id"])), 20, "the OK card")
    [card] = sql("SELECT * FROM mailbox_items WHERE id=$1", w["mailbox_id"])
    assert card["kind"] == "schedule_ok"
    r = httpx.post(f"{stack['orch']}/mailbox/{card['id']}/decide", json={"action": "reject"})
    assert r.status_code == 200 and r.json()["done"].startswith("Skipped this time.")
    assert job(j["id"])["status"] in ("scheduled", "running", "waiting")         # still there: only this one skipped
    assert not [x for x in fn["runs"] if x[1].get("what") == "the gate"]
    w2 = until(lambda: (lambda x: x if x["status"] == "waiting" and x["mailbox_id"] != w["mailbox_id"] else None)(job(j["id"])),
               20, "the next OK card")
    r = httpx.post(f"{stack['orch']}/mailbox/{w2['mailbox_id']}/decide", json={"action": "approve"})
    assert r.status_code == 200
    until(lambda: [x for x in fn["runs"] if x[1].get("what") == "the gate"], 20, "the approved run")
    until(lambda: job(j["id"])["status"] in ("scheduled", "waiting"), 20, "back on schedule")
    spec = job(j["id"])["spec"]
    assert "approved" not in (json.loads(spec) if isinstance(spec, str) else spec)   # the OK was for that run only
    task(stack["orch"], "pulsar.delete", {"which": "gate"})


def test_a_routine_from_his_words_runs_by_its_name_and_on_a_schedule(stack, fn):
    out = ask(stack, "make a routine called night owl: peek the yard, then peek the shed and flip the porch")
    assert out["route"] == "fastpath" and out["needs_confirmation"]
    assert out["reply"].startswith('Routine "night owl": 1) Peek the yard; 2) Peek the shed (after the one before); '
                                   '3) Flip the porch.')
    assert not sql("SELECT 1 FROM skills WHERE name='night owl'")
    done = confirm(stack, out)
    assert done["reply"] == 'Saved. Say "night owl" to run it.'
    [sk] = sql("SELECT * FROM skills WHERE name='night owl' AND status='active'")
    steps = json.loads(sk["steps"])
    assert sk["source"] == "routine" and [s["tool"] for s in steps] == ["pulsarfn.look"] * 2 + ["pulsarfn.act"]
    assert [s["depends"] for s in steps] == [[], [1], []]
    n = len(fn["runs"])
    run = ask(stack, "night owl")
    assert run["route"] == "fastpath" and run["reply"].startswith("Running your skill")
    until(lambda: {"the yard", "the shed", "the porch"} <= {r[1].get("what") for r in fn["runs"][n:]}, 30, "the routine")
    # A routine's name can be an automation's command.
    out, done, j = save(stack, "run night owl every 0.05 minutes")
    assert j["kind"] == "routine" and out["reply"].startswith("Every 0.05 minutes: Run night owl.")
    assert j["heads_up"] is True                                        # it flips something: heads-up first
    until(lambda: any(r["outcome"] == "done" for r in runs(j["id"])), 40, "a routine run")
    # Deleting the routine deletes what ran it.
    t, res = task(stack["orch"], "pulsar.forget_routine", {"which": "the night owl routine"})
    assert t["status"] == "completed" and "Also deleted 1 automation" in res["did"]["what"]
    until(lambda: job(j["id"])["status"] == "cancelled", 20, "the automation ending")   # (a run in progress is its last)
    assert sql("SELECT status FROM skills WHERE id=$1", sk["id"])[0]["status"] == "retired"
    # A name that's already one of my commands isn't taken over by a routine.
    out = ask(stack, "make a routine called flip it: peek the yard")
    assert out["reply"].startswith('Routine "flip it"') and "run flip it" in out["reply"]
    confirm(stack, out, approve=False)
    out = ask(stack, "make a routine called dance: do a backflip")        # not a command I know: the model hears it
    assert not out["needs_confirmation"] or out["route"] != "fastpath"


def test_an_automation_waits_for_a_computer_to_come_online(stack, fn):
    from app import pulsar
    from app.main import app
    node = sql("INSERT INTO devices (kind, name, key_hash, status, info) VALUES ('node', 'Den Box', $1, 'approved', "
               "'{\"hostname\": \"den-box\"}'::jsonb) RETURNING id", uuid.uuid4().hex)[0]["id"]

    async def step():
        return await pulsar.events_step(app, {}) or {}
    _route("/_test/pulsar/events", step)
    r = redis_sync.Redis.from_url(REDIS)
    httpx.post(f"{stack['orch']}/_test/pulsar/events").raise_for_status()      # first look: offline
    out, done, j = save(stack, "when the den box comes online, peek the den")
    assert j["status"] == "armed" and out["reply"].startswith("When Den Box comes online: Peek the den.")
    assert "First run" not in out["reply"]
    r.set(f"aurelia:node:{node}:online", "1", ex=60)
    httpx.post(f"{stack['orch']}/_test/pulsar/events").raise_for_status()      # it came online: the automation runs
    until(lambda: [x for x in fn["runs"] if x[1].get("what") == "the den"], 20, "the event's run")
    until(lambda: job(j["id"])["status"] == "armed" and job(j["id"])["runs"] == 1, 20, "re-armed")
    httpx.post(f"{stack['orch']}/_test/pulsar/events").raise_for_status()      # still online: nothing new
    time.sleep(1.5)
    assert len([x for x in fn["runs"] if x[1].get("what") == "the den"]) == 1
    r.delete(f"aurelia:node:{node}:online")
    httpx.post(f"{stack['orch']}/_test/pulsar/events").raise_for_status()
    assert sql("SELECT 1 FROM devices WHERE id=$1", node)
    out = ask(stack, "when the attic box comes online, peek the attic")       # a computer I don't know
    assert not out["needs_confirmation"] or out["route"] != "fastpath"


def test_how_he_hears_depends_on_the_severity(stack, fn):
    from app import pulsar
    from app.main import app
    rules = app.state.governance.functions["pulsar"].rules
    r = redis_sync.Redis.from_url(REDIS)
    sub = r.pubsub()
    sub.subscribe("aurelia:events")

    async def raise_(body: dict):
        if body.get("clear"):
            return {"cleared": await pulsar.clear(app, body["kind"], body["key"])}
        n = await pulsar.notice(app, body["kind"], body["key"], body["title"], body.get("text", ""))
        return {"id": n["id"] if n else None}

    async def release():
        return {"n": await pulsar.release_held(app)}
    _route("/_test/pulsar/notice", raise_)
    _route("/_test/pulsar/release", release)
    post = lambda **b: httpx.post(f"{stack['orch']}/_test/pulsar/notice", json=b).json()
    try:
        rules["severity"].update(water_leak="critical", computer_down="high", budget_ahead="normal", battery_low="low")
        time.sleep(0.1)
        crit = post(kind="water_leak", key="Sink", title="Water: Sink is wet", text="Check it now.")["id"]
        [n] = sql("SELECT * FROM notices WHERE id=$1", crit)
        [card] = sql("SELECT * FROM mailbox_items WHERE id=$1", n["mailbox_id"])
        assert (card["kind"], card["urgent"], card["source"]) == ("notice", True, "pulsar")
        events, deadline = [], time.time() + 2
        while time.time() < deadline:
            m = sub.get_message(timeout=0.2)
            if m and m["type"] == "message":
                events.append(json.loads(m["data"]))
        shown = [e for e in events if e["tool"] == "pulsar.notice" and e["type"] == "panel.result"]
        assert shown and shown[0]["data"]["notice"]["text"] == "Check it now." and shown[0].get("device") is None
        assert post(kind="water_leak", key="Sink", title="Water: Sink is wet")["id"] == crit       # once while it lasts
        assert sql("SELECT count FROM notices WHERE id=$1", crit)[0]["count"] == 2
        # "Got it" on the card: no more repeats; it closes when the sensor dries.
        assert httpx.post(f"{stack['orch']}/mailbox/{card['id']}/decide", json={"action": "approve"}).json()["done"] \
            .startswith("Got it.")
        assert sql("SELECT seen FROM notices WHERE id=$1", crit)[0]["seen"] is True
        assert post(kind="water_leak", key="Sink", clear=True, title="")["cleared"] is True
        assert sql("SELECT status FROM notices WHERE id=$1", crit)[0]["status"] == "cleared"
        # High during quiet hours: held (a card, not urgent), and his phone hears when they end.
        rules["quiet_hours"] = "00:00-23:59"
        high = post(kind="computer_down", key="node:1", title="Den Box is down")["id"]
        [n] = sql("SELECT * FROM notices WHERE id=$1", high)
        assert n["held"] is True and sql("SELECT urgent FROM mailbox_items WHERE id=$1", n["mailbox_id"])[0]["urgent"] is False
        assert httpx.post(f"{stack['orch']}/_test/pulsar/release").json()["n"] == 0             # still quiet
        rules["quiet_hours"] = ""
        assert httpx.post(f"{stack['orch']}/_test/pulsar/release").json()["n"] == 1
        [n] = sql("SELECT * FROM notices WHERE id=$1", high)
        # Released once, straight to his phone; its card isn't made urgent (the mailbox would push it a second time).
        assert n["held"] is False and sql("SELECT urgent FROM mailbox_items WHERE id=$1", n["mailbox_id"])[0]["urgent"] is False
        # Clearing takes the card back.
        post(kind="computer_down", key="node:1", clear=True, title="")
        assert sql("SELECT status FROM mailbox_items WHERE id=$1", n["mailbox_id"])[0]["status"] == "expired"
        # Normal: a card, never urgent. Low: the digest only.
        norm = post(kind="budget_ahead", key="month:x", title="Budget ahead")["id"]
        [n] = sql("SELECT * FROM notices WHERE id=$1", norm)
        assert sql("SELECT urgent FROM mailbox_items WHERE id=$1", n["mailbox_id"])[0]["urgent"] is False
        low = post(kind="battery_low", key="Front Door", title="Front Door: battery at 9%")["id"]
        assert sql("SELECT mailbox_id, told_at FROM notices WHERE id=$1", low)[0]["mailbox_id"] is None
        t, res = task(stack["orch"], "pulsar.notices", {})
        titles = [x["title"] for x in res["notices"]["open"]]
        assert "Budget ahead" in titles and "Front Door: battery at 9%" in titles
        assert {w["kind"] for w in res["notices"]["watching"]} >= {"door_unlocked_at_night", "computer_down"}
        p = httpx.get(f"{stack['orch']}/pulsar/notices/{norm}/panel").json()
        assert p["tool"] == "pulsar.notice" and p["data"]["notice"]["severity"] == "normal"
    finally:
        rules["quiet_hours"] = ""
        rules["severity"].update(water_leak="low", computer_down="low", budget_ahead="low", battery_low="low")
        sql("UPDATE notices SET status='cleared' WHERE status='open'")


def test_the_home_check_looks_only_and_flags_an_unlocked_door_at_night(stack, fn):
    from app import pulsar
    from app.main import app
    rules = app.state.governance.functions["pulsar"].rules

    async def home():
        await pulsar._watch_home(app)
        return {}
    _route("/_test/pulsar/home", home)
    try:
        rules["severity"]["door_unlocked_at_night"] = "critical"
        fn["devices"][0]["locked"] = False
        httpx.post(f"{stack['orch']}/_test/pulsar/home").raise_for_status()      # daytime: not flagged
        assert not sql("SELECT 1 FROM notices WHERE kind='door_unlocked_at_night' AND status='open'")
        [t] = sql("SELECT user_id, trigger FROM task_runs WHERE tool='orbit.status' ORDER BY started_at DESC LIMIT 1")
        assert (t["user_id"], t["trigger"]) == ("pulsar", "pulsar")
        rules["quiet_hours"] = "00:00-23:59"
        httpx.post(f"{stack['orch']}/_test/pulsar/home").raise_for_status()
        [n] = sql("SELECT * FROM notices WHERE kind='door_unlocked_at_night' AND status='open'")
        assert n["title"] == "Front Door is unlocked" and n["severity"] == "critical"
        fn["devices"][0]["locked"] = True
        httpx.post(f"{stack['orch']}/_test/pulsar/home").raise_for_status()
        assert sql("SELECT status FROM notices WHERE id=$1", n["id"])[0]["status"] == "cleared"
        # Its user can only look: anything that acts is refused for it, whatever the function's policy says.
        gov = app.state.governance
        assert not gov.decide("orbit", "status", "pulsar", False).needs_confirmation
    finally:
        rules["quiet_hours"] = ""
        rules["severity"]["door_unlocked_at_night"] = "low"
        fn["devices"][0]["locked"] = True


def test_parallax_habits_become_automations_when_he_approves_them(stack, fn):
    from app import pulsar
    from app.main import app
    p = {"kind": "automation", "fingerprint": f"automation:t:{uuid.uuid4().hex[:6]}",
         "title": "Peek the mail every weekday at 7:25 AM", "summary": "habit",
         "proposal": {"tool": "pulsarfn.look", "args": {"what": "the mail"},
                      "schedule": {"days": ["mon", "tue", "wed", "thu", "fri"], "time": "07:25", "timezone": "America/Chicago"}},
         "evidence": ["Mon"], "strength": 8, "confidence": 0.8}
    item = httpx.post(f"{stack['orch']}/mailbox/proposals", json=p).json()["id"]
    r = httpx.post(f"{stack['orch']}/mailbox/{item}/decide", json={"action": "modify", "changes": {
        "schedule": {"days": ["mon", "wed"], "time": "08:10"}}})
    assert r.status_code == 200 and r.json()["done"].startswith("Saved: Every Monday and Wednesday at 8:10 AM: Peek the mail.")
    [a] = sql("SELECT * FROM automations WHERE from_item=$1", item)
    j = job(a["job_id"])
    assert a["status"] == "active" and j["title"] == "Peek the mail" and j["status"] == "scheduled"
    # One approved before Pulsar existed is adopted once.
    item2 = httpx.post(f"{stack['orch']}/mailbox/proposals", json={**p, "fingerprint": p["fingerprint"] + "b",
                                                                   "title": "Peek the porch every day at 9:00 PM",
                                                                   "proposal": {**p["proposal"], "args": {"what": "the porch"}}}).json()["id"]
    sql("UPDATE mailbox_items SET status='approved' WHERE id=$1", item2)
    sql("INSERT INTO automations (tool, args, schedule, from_item) VALUES ('pulsarfn.look', '{\"what\": \"the porch\"}', "
        "'{\"days\": [\"sun\"], \"time\": \"21:00\"}', $1)", item2)

    async def adopt():
        return {"n": await pulsar.adopt_waiting(app)}
    _route("/_test/pulsar/adopt", adopt)
    assert httpx.post(f"{stack['orch']}/_test/pulsar/adopt").json()["n"] == 1
    assert httpx.post(f"{stack['orch']}/_test/pulsar/adopt").json()["n"] == 0
    [a2] = sql("SELECT * FROM automations WHERE from_item=$1", item2)
    assert a2["status"] == "active" and job(a2["job_id"])["title"] == "Peek the porch"
    assert sql("SELECT 1 FROM notices WHERE kind='automations_ready'")


def test_something_that_repeats_never_runs_now_or_just_once(stack, fn):
    """A phrasing the automation fast path doesn't take still never becomes a command now, or a one-off."""
    n = len(fn["runs"])
    for text in ("flip the porch every other weeknight at 11",          # not a repeat I can read: the model hears it
                 "flip the porch at 9 pm every second tuesday"):
        out = ask(stack, text)
        assert out["route"] != "fastpath", text
    time.sleep(1)
    assert not [r for r in fn["runs"][n:] if "porch" in str(r[1].get("what"))]
    assert not sql("SELECT 1 FROM jobs WHERE created_by='owner' AND status='scheduled' AND title ILIKE '%porch%'")
    t, _ = task(stack["orch"], "admiral.schedule", {"say": "at 9 pm flip the porch every night"})
    assert t["status"] == "failed" and "automation" in t["error"]
    t, _ = task(stack["orch"], "admiral.schedule", {"when": "in 5 minutes", "tool": "pulsar.save", "args": {"draft": "x"}})
    assert t["status"] == "failed" and "can't schedule" in t["error"]     # an automation is saved only by his yes
    # Day lists and weeknights are read by code.
    out = ask(stack, "flip the porch every monday, wednesday and friday at 9 pm")
    assert out["route"] == "fastpath" and out["reply"].startswith("Every Monday, Wednesday and Friday at 9:00 PM: Flip the porch.")
    confirm(stack, out, approve=False)


def test_a_routine_waiting_on_his_ok_stops_once_its_time_has_passed(stack, fn):
    from app.main import app
    grace = app.state.governance.functions["admiral"].rules["schedule"]["grace_minutes"]
    old = grace.get("action")
    grace["action"] = 0.05                                                # 3 seconds, for the test
    try:
        out = ask(stack, "make a routine called lockup: peek the hall, then unbolt the cellar")
        confirm(stack, out)
        out, done, j = save(stack, "run lockup every 0.05 minutes")
        r = until(lambda: [x for x in runs(j["id"]) if x["outcome"] == "missed"], 40, "the missed run")[0]
        assert "past its time" in r["note"]
        [plan] = sql("SELECT id, status FROM plans WHERE task_id=$1", r["task_id"])
        assert plan["status"] == "stopped"                               # never runs late
        cards = sql("SELECT s.status, m.status AS card FROM plan_steps s LEFT JOIN mailbox_items m ON m.id = s.mailbox_id "
                    "WHERE s.plan_id=$1 AND s.tool='pulsarfn.confirm'", plan["id"])
        assert cards and cards[0]["status"] == "skipped" and cards[0]["card"] in (None, "expired")
        # His OK arriving now is too late: the stopped plan stays stopped and the step never runs.
        [step] = sql("SELECT n FROM plan_steps WHERE plan_id=$1 AND tool='pulsarfn.confirm'", plan["id"])
        late = httpx.post(f"{stack['orch']}/admiral/plans/{plan['id']}/steps/{step['n']}/decide", json={"approve": True})
        assert late.status_code == 409
        assert sql("SELECT status FROM plans WHERE id=$1", plan["id"])[0]["status"] == "stopped"
        assert not [x for x in fn["runs"] if x[1].get("what") == "the cellar"]
        assert job(j["id"])["status"] in ("scheduled", "running")         # the automation carries on
        task(stack["orch"], "pulsar.delete", {"which": "lockup"})
        task(stack["orch"], "pulsar.forget_routine", {"which": "lockup"})
    finally:
        grace["action"] = old


def test_the_display_tape_shows_what_will_run_in_the_next_day(stack, fn):
    tz = ZoneInfo("America/Chicago")
    soon = (datetime.now(tz) + timedelta(hours=2)).replace(second=0, microsecond=0)
    words = soon.strftime("%I:%M %p").lstrip("0").lower()
    _, done, j = save(stack, f"flip the porch light every day at {words}")
    tape = httpx.get(f"{stack['orch']}/view/tape", timeout=10).json()
    assert tape["tz"] == "America/Chicago" and "quiet" in tape and "items" in tape
    mine = [i for i in tape["items"] if i["job"] == j["id"]]
    assert len(mine) == 1, tape                                         # every day: once in the next 24 hours
    [it] = mine
    assert it["automation"] and it["repeats"] and not it["waiting"]
    assert datetime.fromisoformat(it["at"]) == j["planned_at"]          # where it falls is when it will run
    assert it["title"] == j["title"]
    # Cancelled: it leaves the tape.
    sql("UPDATE jobs SET status='cancelled' WHERE id=$1", j["id"])
    tape = httpx.get(f"{stack['orch']}/view/tape", timeout=10).json()
    assert not [i for i in tape["items"] if i["job"] == j["id"]]
