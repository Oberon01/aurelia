"""Admiral end to end: a request becomes a plan of governed steps that runs (in parallel where it can),
counts a step done only from its own result, waits for his OK on a change-tier step, retries once,
survives a restart, stops at his word, and ends in a report checked against what he asked.
Real Postgres and Redis; the model is scripted and the steps' function is a stand-in."""
import asyncio
import json
import re
import threading
import time
import uuid

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


class Model:
    """Plans by goal; fills and checks from what the test sets."""
    def __init__(self):
        self.plans, self.fills, self.reports, self.calls = {}, {}, {}, []

    async def complete(self, route, system, messages, tools):
        from app import admiral
        from app.llm.base import Completion, Usage
        text = messages[-1].text
        self.calls.append((system[:20], text))
        if system.startswith(admiral.PLAN_SYSTEM[:40]):
            goal = re.search(r"His request: (.*?)\n\nTools:", text, re.S).group(1)
            out = self.plans[goal]
        elif system == admiral.FILL_SYSTEM:
            out = self.fills[re.search(r"Goal: (.*?)\n", text).group(1)]
        else:
            goal = re.search(r"What he asked: (.*?)\n", text).group(1)
            out = self.reports.get(goal) or {"headline": f"Answer for {goal}", "summary": "s", "meets_goal": True, "gaps": []}
        return Completion(text=out if isinstance(out, str) else json.dumps(out), tool_calls=[], usage=Usage(10, 5),
                          model="fake-plan", provider="fake")


@pytest.fixture(scope="module")
def planfn(stack):
    """A function whose steps do what each test needs: look (with a delay), act (confirmed or not),
    flaky (fails its first try), broken (always fails), confirm (change tier)."""
    import uvicorn
    from fastapi import FastAPI
    orch, port = stack["orch"], free_port()
    fn = FastAPI()
    seen = {"runs": [], "flaky": set()}

    @fn.post("/tools/{tool}/invoke", status_code=202)
    async def invoke(tool: str, body: dict):
        args = body.get("args") or {}

        async def work():
            t0 = time.time()
            await asyncio.sleep(float(args.get("delay") or 0.05))
            seen["runs"].append((tool, args, t0, time.time()))
            if tool == "broken" or (tool == "flaky" and args.get("key") not in seen["flaky"]):
                seen["flaky"].add(args.get("key"))
                res = {"status": "failed", "error": f"{tool} went wrong"}
            elif tool == "act":
                res = {"status": "completed", "data": {"did": {"node": "Den PC", "what": "asked",
                                                                **({"unconfirmed": True} if args.get("unconfirmed") else {})}}}
            else:
                res = {"status": "completed", "data": {"found": args.get("q") or args.get("what") or tool}}
            async with httpx.AsyncClient() as c:
                await c.post(f"{orch}/tasks/{body['task_id']}/result", json=res)
        asyncio.create_task(work())
        return {}

    srv = uvicorn.Server(uvicorn.Config(fn, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    time.sleep(0.5)
    url = f"http://127.0.0.1:{port}"

    def register():
        prop = lambda *ks: {"type": "object", "properties": {k: {"type": "string"} for k in ks}}
        httpx.post(f"{orch}/registry/functions", json={"name": "planfn", "description": "test", "url": url, "tools": [
            {"name": "look", "description": "look something up", "input_schema": prop("q", "delay")},
            {"name": "act", "description": "do something", "input_schema": prop("what", "unconfirmed")},
            {"name": "flaky", "description": "flaky", "input_schema": prop("key")},
            {"name": "broken", "description": "broken", "input_schema": prop()},
            {"name": "confirm", "description": "needs his OK", "input_schema": prop("what")}]}).raise_for_status()
    seen["register"] = register
    yield seen
    srv.should_exit = True


@pytest.fixture
def model(stack, planfn):
    from app.main import app
    planfn["register"]()
    deadline = time.time() + 20                         # Admiral registers itself at start-up
    while time.time() < deadline and not any(f["name"] == "admiral" for f in httpx.get(f"{stack['orch']}/registry/functions").json()):
        time.sleep(0.2)
    original, fake = app.state.llm, Model()
    app.state.llm = fake
    yield fake
    app.state.llm = original


def make_plan(orch, goal):
    tid = httpx.post(f"{orch}/tasks", json={"tool": "admiral.plan", "args": {"goal": goal}}).json()["task_id"]
    deadline = time.time() + 15
    while time.time() < deadline:
        t = httpx.get(f"{orch}/tasks/{tid}").json()
        if t["status"] != "running":
            res = json.loads(t["result"]) if isinstance(t["result"], str) else t["result"]
            return t, res
        time.sleep(0.1)
    raise AssertionError("admiral.plan never finished")


def plan_row(plan_id):
    [p] = sql("SELECT * FROM plans WHERE id=$1", uuid.UUID(plan_id))
    steps = sql("SELECT * FROM plan_steps WHERE plan_id=$1 ORDER BY n", uuid.UUID(plan_id))
    return dict(p), [dict(s) for s in steps]


def until(plan_id, statuses, wait=30):
    deadline = time.time() + wait
    while time.time() < deadline:
        p, steps = plan_row(plan_id)
        if p["status"] in statuses:
            return p, steps
        time.sleep(0.2)
    raise AssertionError(f"plan stayed {p['status']}: {[(s['n'], s['status'], s['note']) for s in steps]}")


def step(n, tool, args=None, depends=(), fill=False, title=None):
    return {"n": n, "title": title or f"step {n}", "tool": tool, "args": args or {}, "depends": list(depends), "fill": fill}


def test_a_plan_runs_in_parallel_fills_from_results_and_ends_in_a_checked_report(stack, model, planfn):
    orch, goal = stack["orch"], "compare two laptops"
    model.plans[goal] = "```json\n" + json.dumps({"steps": [
        step(1, "planfn.look", {"q": "laptop a", "delay": "0.8"}),
        step(2, "planfn.look", {"q": "laptop b", "delay": "0.8"}),
        step(3, "planfn.act", {"what": "guess"}, depends=[1, 2], fill=True, title="Show the winner")],
        "report": "which is better"}) + "\n```"
    model.fills[goal] = {"what": "laptop a wins", "not_in_schema": 1}
    model.reports[goal] = {"headline": "Laptop A is the better buy", "summary": "A beats B.", "meets_goal": True, "gaps": []}
    task, res = make_plan(orch, goal)
    assert task["status"] == "completed", task["error"]
    assert res["headline"] == "Plan · 3 steps"
    pid = res["plan"]["id"]
    assert [s["status"] for s in res["plan"]["steps"]][2] == "pending"
    p, steps = until(pid, {"done", "gaps", "failed"})
    assert p["status"] == "done", (p, steps)
    assert all(s["status"] == "done" for s in steps)
    report = json.loads(p["report"])
    assert report["headline"] == "Laptop A is the better buy" and report["meets_goal"] is True
    assert json.loads(steps[2]["args"]) == {"what": "laptop a wins"}           # filled, only what the tool takes
    runs = {a.get("q") or a.get("what"): (t0, t1) for tool, a, t0, t1 in planfn["runs"] if tool in ("look", "act")}
    (a0, a1), (b0, b1), (c0, _) = runs["laptop a"], runs["laptop b"], runs["laptop a wins"]
    assert a0 < b1 and b0 < a1                                             # 1 and 2 ran at the same time
    assert c0 >= max(a1, b1) - 0.05                                        # 3 waited for both
    # every step went through the task path (governed, logged), as Admiral's own
    trig = sql("SELECT trigger, tool FROM task_runs WHERE id = ANY($1::uuid[])", [s["task_id"] for s in steps])
    assert {r["trigger"] for r in trig} == {"admiral"}
    assert sql("SELECT count(*) AS n FROM llm_calls WHERE request_id=$1", uuid.UUID(pid))[0]["n"] == 3   # plan, fill, check


def test_an_unconfirmed_step_is_not_done_and_blocks_what_needs_it(stack, model, planfn):
    orch, goal = stack["orch"], "open it and then read it"
    model.plans[goal] = {"steps": [step(1, "planfn.act", {"what": "open", "unconfirmed": "yes"}),
                                   step(2, "planfn.look", {"q": "after open"}, depends=[1]),
                                   step(3, "planfn.look", {"q": "independent"})]}
    model.reports[goal] = {"headline": "Partly done", "summary": "Read the independent one.", "meets_goal": True, "gaps": []}
    _, res = make_plan(orch, goal)
    p, steps = until(res["plan"]["id"], {"done", "gaps", "failed"})
    assert [s["status"] for s in steps] == ["unconfirmed", "skipped", "done"]
    assert steps[0]["note"] == "asked" and p["status"] == "gaps"           # never "done" on a sent command
    gaps = json.loads(p["report"])["gaps"]
    assert any("Step 1" in g and "unconfirmed" in g for g in gaps)
    assert not [r for r in planfn["runs"] if r[1].get("q") == "after open"]   # step 2 never ran


def test_a_failed_step_is_tried_once_more_then_the_plan_stops_there(stack, model, planfn):
    orch, goal = stack["orch"], "try the flaky and the broken"
    model.plans[goal] = {"steps": [step(1, "planfn.flaky", {"key": goal}), step(2, "planfn.broken"),
                                   step(3, "planfn.look", {"q": "x"}, depends=[2])]}
    _, res = make_plan(orch, goal)
    p, steps = until(res["plan"]["id"], {"done", "gaps", "failed"})
    assert (steps[0]["status"], steps[0]["attempts"]) == ("done", 2)
    assert (steps[1]["status"], steps[1]["attempts"], steps[1]["note"]) == ("failed", 2, "broken went wrong")
    assert steps[2]["status"] == "skipped" and p["status"] == "gaps"


def test_a_step_that_needs_his_ok_waits_and_his_answer_resumes_the_plan(stack, model, planfn):
    orch = stack["orch"]
    for goal, approve in (("send it after looking", True), ("send it but I say no", False)):
        model.plans[goal] = {"steps": [step(1, "planfn.look", {"q": "first"}),
                                       step(2, "planfn.confirm", {"what": "the thing"}, depends=[1]),
                                       step(3, "planfn.look", {"q": "after"}, depends=[2])]}
        _, res = make_plan(orch, goal)
        pid = res["plan"]["id"]
        p, steps = until(pid, {"waiting", "failed"})
        assert p["status"] == "waiting" and steps[1]["status"] == "waiting" and steps[1]["attempts"] == 0
        assert steps[2]["status"] == "pending"
        assert httpx.post(f"{orch}/admiral/plans/{pid}/steps/1/decide", json={"approve": True}).status_code == 409
        r = httpx.post(f"{orch}/admiral/plans/{pid}/steps/2/decide", json={"approve": approve})
        assert r.status_code == 200
        p, steps = until(pid, {"done", "gaps", "failed"})
        if approve:
            assert [s["status"] for s in steps] == ["done", "done", "done"] and p["status"] == "done"
            [run] = sql("SELECT confirmed FROM task_runs WHERE id=$1", steps[1]["task_id"])
            assert run["confirmed"] is True                                  # it ran as the step he approved
        else:
            assert [s["status"] for s in steps] == ["done", "skipped", "skipped"] and p["status"] == "gaps"
    assert httpx.post(f"{orch}/admiral/plans/{uuid.uuid4()}/steps/1/decide", json={"approve": True}).status_code == 404


def test_a_plan_picks_up_where_it_was_after_a_restart(stack, model, planfn):
    from app import admiral
    from app.main import app
    orch, goal = stack["orch"], "survive a restart"
    if not any(getattr(r, "path", "") == "/_test/admiral/resume" for r in app.routes):
        async def resume():
            await admiral.resume_all(app)
            return {}
        app.add_api_route("/_test/admiral/resume", resume, methods=["POST"])
    # a step that was running when I stopped (its task finished meanwhile), one never started, one pending
    done_task = httpx.post(f"{orch}/tasks", json={"tool": "planfn.look", "args": {"q": "before restart"}}).json()["task_id"]
    time.sleep(0.5)
    pid = uuid.uuid4()
    sql("INSERT INTO plans (id, goal, status) VALUES ($1, $2, 'running')", pid, goal)
    for n, status, task, deps in ((1, "running", uuid.UUID(done_task), []), (2, "running", None, []), (3, "pending", None, [1, 2])):
        sql("INSERT INTO plan_steps (plan_id, n, title, tool, args, depends, status, task_id, attempts) "
            "VALUES ($1, $2, $3, 'planfn.look', $4::jsonb, $5, $6, $7, $8)",
            pid, n, f"s{n}", json.dumps({"q": f"r{n}"}), deps, status, task, 1 if status == "running" else 0)
    httpx.post(f"{orch}/_test/admiral/resume").raise_for_status()
    p, steps = until(str(pid), {"done", "gaps", "failed"})
    assert p["status"] == "done" and [s["status"] for s in steps] == ["done", "done", "done"]
    assert json.loads(steps[0]["result"]) == {"found": "before restart"}          # its own result, not run again
    assert steps[1]["attempts"] == 1


def test_he_can_stop_a_plan_and_nothing_else_starts(stack, model, planfn):
    orch, goal = stack["orch"], "a long one to stop"
    model.plans[goal] = {"steps": [step(1, "planfn.look", {"q": "slow", "delay": "2"}),
                                   step(2, "planfn.look", {"q": "never"}, depends=[1])]}
    _, res = make_plan(orch, goal)
    pid = res["plan"]["id"]
    time.sleep(0.5)
    tid = httpx.post(f"{orch}/tasks", json={"tool": "admiral.stop", "args": {}}).json()["task_id"]
    deadline = time.time() + 10
    while httpx.get(f"{orch}/tasks/{tid}").json()["status"] == "running" and time.time() < deadline:
        time.sleep(0.1)
    p, steps = plan_row(pid)
    assert p["status"] == "stopped" and steps[1]["status"] == "skipped"
    time.sleep(2.5)
    p, steps = plan_row(pid)
    assert p["status"] == "stopped" and steps[1]["status"] == "skipped"
    assert not [r for r in planfn["runs"] if r[1].get("q") == "never"]
    plans = httpx.post(f"{orch}/tasks", json={"tool": "admiral.plans", "args": {}}).json()["task_id"]
    time.sleep(1)
    out = httpx.get(f"{orch}/tasks/{plans}").json()
    items = (json.loads(out["result"]) if isinstance(out["result"], str) else out["result"])["plans"]["items"]
    assert any(i["id"] == pid and i["status"] == "stopped" for i in items)


def test_a_plan_the_model_gets_wrong_never_runs(stack, model, planfn):
    orch, goal = stack["orch"], "do something impossible"
    model.plans[goal] = {"steps": [step(1, "planfn.launch_missiles")]}
    task, _ = make_plan(orch, goal)
    assert task["status"] == "failed" and "couldn't make a plan" in task["error"] and "launch_missiles" in task["error"]
    [p] = sql("SELECT status FROM plans WHERE goal=$1", goal)
    assert p["status"] == "failed" and not sql("SELECT 1 FROM plan_steps s JOIN plans p ON p.id=s.plan_id WHERE p.goal=$1", goal)
