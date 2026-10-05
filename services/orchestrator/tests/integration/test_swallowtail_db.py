"""Swallowtail: field checks run on the live system after a release, safely, and report what actually happened."""
import asyncio
import json
import threading
import time
import uuid
from pathlib import Path

import asyncpg
import httpx
import pytest

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


def _route(path, fn_, method="POST"):
    from app.main import app
    if not any(getattr(r, "path", "") == path for r in app.routes):
        app.add_api_route(path, fn_, methods=[method])


def until(cond, wait=30, what="it"):
    deadline = time.time() + wait
    while time.time() < deadline:
        v = cond()
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError(f"{what} never happened")


@pytest.fixture(scope="module")
def home(stack, tmp_path_factory):
    """A pretend home: a lamp that reports its state, a look-up, an action and a change-tier one."""
    import uvicorn
    from fastapi import FastAPI
    from app.main import app
    from app import swallowtail
    orch, port = stack["orch"], free_port()
    fn, seen = FastAPI(), {"runs": [], "lamp": False}

    @fn.post("/tools/{tool}/invoke", status_code=202)
    async def invoke(tool: str, body: dict):
        args = body.get("args") or {}

        async def work():
            seen["runs"].append((tool, args))
            if tool == "flip":
                seen["lamp"] = args.get("on", "on") == "on"
            data = {"devices": [{"name": "Porch Lamp", "kind": "light", "on": seen["lamp"]}]} if tool == "status" else \
                {"did": {"node": "Home", "what": f"{tool} {args}"}, "headline": f"Did {tool}", "echo": args}
            async with httpx.AsyncClient() as c:
                await c.post(f"{orch}/tasks/{body['task_id']}/result", json={"status": "completed", "data": data})
        asyncio.create_task(work())
        return {}

    srv = uvicorn.Server(uvicorn.Config(fn, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    time.sleep(0.5)
    gov = Path(app.state.governance.root) / "functions"
    (gov / "homefn.yaml").write_text("function: homefn\nrate_limit_per_minute: 1000\ntools:\n  status: {tier: reference}\n"
                                     "  peek: {tier: reference}\n  flip: {tier: act}\n  bolt: {tier: change}\n")
    app.state.governance.load_all()

    def register():
        prop = {"type": "object", "properties": {"what": {"type": "string"}, "on": {"type": "string"}}}
        httpx.post(f"{orch}/registry/functions", json={"name": "homefn", "description": "test", "url": f"http://127.0.0.1:{port}",
                   "tools": [{"name": t, "description": t, "input_schema": prop} for t in ("status", "peek", "flip", "bolt")]}
                   ).raise_for_status()
    seen["register"] = register
    checks = tmp_path_factory.mktemp("checks")
    real_dir = swallowtail.checks_dir
    swallowtail.checks_dir = lambda app_: checks
    seen["dir"] = checks

    async def sweep_now():
        await swallowtail.sweep(app, {})
        return {"ok": True}
    _route("/_test/swallowtail/sweep", sweep_now)
    yield seen
    swallowtail.checks_dir = real_dir
    srv.should_exit = True


@pytest.fixture
def fn(stack, home):
    from app.main import app
    from app.config import FastPathPattern
    home["register"]()
    home["lamp"] = False
    cfg = app.state.config.current.fastpath
    old = cfg.patterns
    real = __import__("yaml").safe_load(Path(__file__).parents[4].joinpath("config", "aurelia.yaml").read_text())["fastpath"]["patterns"]
    pulsar_paths = [FastPathPattern(**p) for p in real if p["tool"].startswith("pulsar.")]
    cfg.patterns = [*pulsar_paths, FastPathPattern(name="t-peek", match=r"^peek (?P<what>.+)$", tool="homefn.peek", args={"what": "{what}"}),
                    FastPathPattern(name="t-flip", match=r"^flip (?P<what>.+?) (?P<on>on|off)$", tool="homefn.flip",
                                    args={"what": "{what}", "on": "{on}"}, reply="Flipped."),
                    FastPathPattern(name="t-bolt", match=r"^bolt (?P<what>.+)$", tool="homefn.bolt", args={"what": "{what}"}),
                    *[p for p in old if p.tool.startswith("swallowtail.")]]
    for f in home["dir"].glob("*.yaml"):
        f.unlink()
    rules = app.state.governance.functions["pulsar"].rules
    q = rules.get("quiet_hours")
    rules["quiet_hours"] = ""
    yield home
    cfg.patterns = old
    rules["quiet_hours"] = q


def write(home, name, text):
    (home["dir"] / name).write_text(text)


def task(orch, tool, args, wait=20):
    tid = httpx.post(f"{orch}/tasks", json={"tool": tool, "args": args}).json()["task_id"]
    deadline = time.time() + wait
    while time.time() < deadline:
        t = httpx.get(f"{orch}/tasks/{tid}").json()
        if t["status"] != "running":
            return t, (json.loads(t["result"]) if isinstance(t["result"], str) else t["result"])
        time.sleep(0.1)
    raise AssertionError(f"{tool} never finished")


def results(run_id):
    return {r["check_id"]: dict(r) for r in sql("SELECT * FROM check_results WHERE run_id=$1", run_id)}


def run_settled(run_id, wait=40):
    return until(lambda: (lambda r: r if r and r[0]["status"] in ("waiting", "done") else None)(
        sql("SELECT * FROM check_runs WHERE id=$1", run_id)), wait, "the run settling")[0]


FILE = """
release: Test release
checks:
  - id: look
    name: A look-up works
    say: "peek the yard"
    expect: { route: fastpath, tool: homefn.peek, status: completed, result: { echo.what: the yard } }
  - id: asks
    name: A change is asked for, never confirmed
    say: "bolt the gate"
    expect: { tool: none, asks_to_confirm: homefn.bolt }
  - id: sneaky
    name: An action not marked as one still waits for Start
    say: "flip the porch lamp on"
    expect: { tool: homefn.flip }
  - id: wrong
    name: A wrong expectation fails, saying what happened
    say: "peek the shed"
    expect: { reply_has: "something it never says" }
  - id: unset
    name: A name you haven't set is skipped
    say: "peek the {garage}"
    expect: { tool: homefn.peek }
  - id: model
    name: The model isn't called
    say: "tell me a story about moths"
    expect: { route: llm, tool: none }
  - id: lamp
    name: The lamp turns on and reports it
    say: "flip the porch lamp on"
    acts: true
    expect: { tool: homefn.flip, status: completed }
    verify: { tool: homefn.status, find: { list: devices, name: porch lamp }, field: "on", equals: true, within: 10s }
    after:
      - say: "flip the porch lamp off"
  - id: you
    name: Something only you can judge
    say: "peek the porch"
    acts: true
    expect: { status: completed }
    question: "Did the porch light look right?"
"""


def test_a_release_runs_its_checks_once_and_on_their_own_they_only_look(stack, fn):
    write(fn, "test-release.yaml", FILE)
    rel = sql("INSERT INTO releases (sha, author, message, changed_paths, services, plan, status, started_at, finished_at) "
              "VALUES ($1, 'k', 'Test release', $2, '{}', '{}'::jsonb, 'live', now() - interval '2 minutes', "
              "now() - interval '1 minute') RETURNING id", uuid.uuid4().hex, ["config/checks/test-release.yaml", "README.md"])[0]["id"]
    n_conf = sql("SELECT count(*) AS n FROM confirmations")[0]["n"]
    httpx.post(f"{stack['orch']}/_test/swallowtail/sweep").raise_for_status()
    httpx.post(f"{stack['orch']}/_test/swallowtail/sweep").raise_for_status()          # once, however often it looks
    runs = sql("SELECT * FROM check_runs WHERE release_id=$1", rel)
    assert len(runs) == 1 and runs[0]["files"] == ["test-release.yaml"]
    run = run_settled(runs[0]["id"])
    assert run["status"] == "waiting"                                     # some wait for his Start
    r = results(run["id"])
    assert r["look"]["status"] == "passed"
    lines = r["look"]["lines"] if isinstance(r["look"]["lines"], list) else json.loads(r["look"]["lines"])
    assert {x["what"] for x in lines} >= {"route", "tool it ran", "result", "result echo.what"} and all(x["ok"] for x in lines)
    assert r["asks"]["status"] == "passed"
    assert r["sneaky"]["status"] == "needs_start" and "would act" in r["sneaky"]["note"]
    assert r["wrong"]["status"] == "failed"
    wl = json.loads(r["wrong"]["lines"])
    assert wl[0]["ok"] is False and wl[0]["expected"] == "something it never says" and wl[0]["actual"]
    assert r["unset"]["status"] == "skipped" and "garage" in r["unset"]["note"]
    assert r["model"]["status"] == "passed" and json.loads(r["model"]["actual"])["model_called"] is False
    assert r["lamp"]["status"] == "needs_start" and r["you"]["status"] == "needs_start"
    # Nothing acted, nothing was confirmed, his earned trust wasn't touched, and none of it is "his" request.
    assert not [x for x in fn["runs"] if x[0] in ("flip", "bolt")] and fn["lamp"] is False
    assert sql("SELECT count(*) AS n FROM confirmations")[0]["n"] == n_conf
    trig = {x["trigger"] for x in sql("SELECT trigger FROM task_runs WHERE tool LIKE 'homefn.%' AND started_at > now() - interval '2 minutes'")}
    assert trig == {"check"}
    assert not sql("SELECT 1 FROM ask_requests WHERE text IN ('peek the yard', 'bolt the gate')")
    # A failure is a notice (his severity), with a rollback offered, never done.
    [nt] = sql("SELECT * FROM notices WHERE kind='release_check_failed' AND key LIKE $1", f"checks:{run['id']}:%")
    assert "1 of 8 checks failed" in nt["title"] and "roll back" in nt["text"]
    # The report: each check's input, what was expected and what actually happened.
    t, res = task(stack["orch"], "swallowtail.checks", {"release": str(rel)})
    assert t["status"] == "completed"
    c = res["checks"]
    assert c["can_start"] is True and c["items"][0]["status"] == "failed"         # failures first
    w = next(i for i in c["items"] if i["check"] == "wrong")
    assert w["input"] == {"say": "peek the shed"} and w["expected"] == {"reply_has": "something it never says"}
    # His history of the release says how its checks went.
    t, res = task(stack["orch"], "mnemosyne.history", {"release": str(rel)})
    assert res["history_release"]["field_checks"]["failed"] == ["A wrong expectation fails, saying what happened"]


def test_his_start_runs_the_ones_that_act_checked_by_what_the_device_reports(stack, fn):
    from app.main import app
    write(fn, "act.yaml", FILE)
    t, res = task(stack["orch"], "swallowtail.run", {"which": "act"})
    assert t["status"] == "completed" and "Running 8 checks" in res["did"]["what"]
    run = until(lambda: sql("SELECT * FROM check_runs WHERE title LIKE '%act%' ORDER BY id DESC LIMIT 1"), 10, "the run")[0]
    run = run_settled(run["id"])
    # Not during quiet hours.
    app.state.governance.functions["pulsar"].rules["quiet_hours"] = "00:00-23:59"
    r = httpx.post(f"{stack['orch']}/swallowtail/runs/{run['id']}/start")
    assert r.status_code == 409 and "quiet hours" in r.json()["detail"]
    app.state.governance.functions["pulsar"].rules["quiet_hours"] = ""
    r = httpx.post(f"{stack['orch']}/swallowtail/runs/{run['id']}/start")
    assert r.status_code == 200 and r.json()["done"] == "Starting 3 checks."
    assert httpx.post(f"{stack['orch']}/swallowtail/runs/{run['id']}/start").status_code == 409     # once
    # The question waits for him.
    q = until(lambda: (lambda x: x if x["you"]["status"] == "needs_you" and x["you"]["asking"] == "ask" else None)(
        results(run["id"])), 40, "the question")["you"]
    assert q["question"] == "Did the porch light look right?"
    assert httpx.post(f"{stack['orch']}/swallowtail/results/{q['id']}/answer", json={"yes": True}).json()["ok"] is True
    run = until(lambda: (lambda x: x if x[0]["status"] == "done" else None)(
        sql("SELECT * FROM check_runs WHERE id=$1", run["id"])), 40, "the run finishing")[0]
    r = results(run["id"])
    assert r["lamp"]["status"] == "passed", r["lamp"]
    ll = json.loads(r["lamp"]["lines"])
    assert ll[-1]["what"] == "porch lamp: on (as it reports)" and ll[-1]["actual"] == "true"
    assert "Cleaned up" in r["lamp"]["note"] and fn["lamp"] is False                      # put back as it was
    assert r["you"]["status"] == "passed" and r["sneaky"]["status"] == "passed"
    assert ("bolt", {"what": "the gate"}) not in fn["runs"]                               # still never confirmed


def test_a_bad_checks_file_says_what_is_wrong(stack, fn):
    write(fn, "bad.yaml", "release: Bad\nchecks:\n  - id: x\n    name: nothing\n    expect: { tool: a }\n    colour: blue\n")
    t, _ = task(stack["orch"], "swallowtail.run", {"which": "bad"})
    run = run_settled(sql("SELECT id FROM check_runs ORDER BY id DESC LIMIT 1")[0]["id"])
    [r] = results(run["id"]).values()
    assert r["status"] == "error" and "bad.yaml" in r["note"]


def test_a_check_watches_for_the_automation_he_makes_and_deletes_only_that_one(stack, fn):
    from app.main import app
    orch = stack["orch"]
    sql("UPDATE jobs SET status='cancelled' WHERE created_by='owner' AND status IN ('scheduled','waiting','armed','paused')")

    def save(text):
        out = httpx.post(f"{orch}/ask", json={"text": text}, timeout=30).json()
        [a] = out["needs_confirmation"]
        return httpx.post(f"{orch}/ask/confirm", json={"action_id": a["action_id"], "approve": True}, timeout=30).json()
    save("flip the porch lamp off every day at 6 am")                 # one of his own, made before the check
    [mine] = sql("SELECT id FROM jobs WHERE created_by='owner' AND status='scheduled' AND title ILIKE '%porch lamp%'")
    write(fn, "watch.yaml", """
release: Watch
checks:
  - id: auto
    name: His automation runs, and only the test one is deleted after
    needs_you: "Say “flip the porch lamp on every 0.05 minutes” and confirm it."
    verify: { automation: "porch lamp", outcome: done, within: 40s }
    question: "Did it look right?"
    after:
      - delete: automation
      - say: "flip the porch lamp off"
""")
    t, _ = task(orch, "swallowtail.run", {"which": "watch"})
    run = run_settled(sql("SELECT id FROM check_runs ORDER BY id DESC LIMIT 1")[0]["id"])
    assert httpx.post(f"{orch}/swallowtail/runs/{run['id']}/start").status_code == 200
    r = until(lambda: (lambda x: x if x["auto"]["status"] == "needs_you" else None)(results(run["id"])), 20, "the instruction")["auto"]
    assert r["asking"] == "info" and "every 0.05 minutes" in r["question"]       # shown while I watch for it
    save("flip the porch lamp on every 0.05 minutes")
    q = until(lambda: (lambda x: x if x["auto"]["asking"] == "ask" else None)(results(run["id"])), 60, "the question")["auto"]
    late = httpx.post(f"{orch}/swallowtail/results/{q['id']}/answer", json={"yes": True, "asking": "do"})
    assert late.status_code == 409                                                # a tap meant for another prompt
    assert httpx.post(f"{orch}/swallowtail/results/{q['id']}/answer", json={"yes": True, "asking": "ask"}).status_code == 200
    until(lambda: sql("SELECT status FROM check_runs WHERE id=$1", run["id"])[0]["status"] == "done", 30, "done")
    r = results(run["id"])["auto"]
    assert r["status"] == "passed", r
    assert "deleted the test automation" in r["note"]
    assert sql("SELECT status FROM jobs WHERE id=$1", mine["id"])[0]["status"] == "scheduled"     # his, untouched
    assert not sql("SELECT 1 FROM jobs WHERE created_by='owner' AND status IN ('scheduled','running') "
                   "AND title ILIKE '%lamp on%'")                                                 # the test one, gone


def test_a_run_cut_off_by_a_restart_picks_up_its_look_only_checks(stack, fn):
    write(fn, "cut.yaml", FILE)
    sql("UPDATE check_runs SET status='done' WHERE status IN ('running','waiting')")
    from app import swallowtail
    run_id = sql("INSERT INTO check_runs (title, files, by_whom, status, updated_at) VALUES ('Cut', '{cut.yaml}', 'you', "
                 "'running', now() - interval '10 minutes') RETURNING id")[0]["id"]
    for i, (cid, spec) in enumerate((("look", {"id": "look", "name": "looks", "say": "peek the yard",
                                                 "expect": {"tool": "homefn.peek"}}),
                                     ("mid", {"id": "mid", "name": "was running", "say": "peek the shed",
                                              "expect": {"tool": "homefn.peek"}}))):
        sql("INSERT INTO check_results (run_id, file, check_id, name, needs, status, ord, spec) VALUES ($1, 'cut.yaml', $2, $3, "
            "'auto', $4, $5, $6::jsonb)", run_id, cid, spec["name"], "pending" if cid == "look" else "running", i, json.dumps(spec))
    httpx.post(f"{stack['orch']}/_test/swallowtail/sweep").raise_for_status()
    until(lambda: sql("SELECT status FROM check_runs WHERE id=$1", run_id)[0]["status"] == "done", 30, "the run picked up")
    r = results(run_id)
    assert r["look"]["status"] == "passed" and r["mid"]["status"] == "skipped" and "restart" in r["mid"]["note"]
