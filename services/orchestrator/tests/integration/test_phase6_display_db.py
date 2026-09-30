"""The display's intelligence on the server side: brief answers, what's worth a glance,
and what Kentrell usually asks next. Real Postgres and Redis."""
import asyncio
import json
import time
import uuid

import asyncpg
import httpx
import pytest
import redis as redis_sync

from .conftest import DB, REDIS

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
    def __init__(self, steps):
        self.steps, self.tools = steps, []

    async def complete(self, route, system, messages, tools):
        from app.llm.base import Completion, ToolCall, Usage
        self.tools = tools
        step = self.steps.pop(0) if self.steps else {"text": "Okay."}
        calls = [ToolCall(id=f"t{uuid.uuid4().hex[:8]}", name=c[0], args=c[1]) for c in step.get("calls", [])]
        return Completion(text=step.get("text", ""), tool_calls=calls, usage=Usage(10, 5), model="fake", provider="fake")


@pytest.fixture
def use_model(stack):
    from app.main import app
    original = app.state.llm

    def install(steps):
        app.state.llm = Model(steps)
        return app.state.llm
    yield install
    app.state.llm = original


def test_a_brief_answer_opens_no_panel(stack, use_model):
    sub = redis_sync.Redis.from_url(REDIS).pubsub()
    sub.subscribe("aurelia:events")
    model = use_model([{"calls": [("testfn__status", {"_display": "brief"})]}, {"text": "Yes, it's locked."}])
    out = httpx.post(f"{stack['orch']}/ask", json={"text": "is the front door locked?", "session_id": uuid.uuid4().hex},
                     timeout=30).json()
    assert out["reply"] == "Yes, it's locked." and len(out["task_ids"]) == 1
    schema = next(t for t in model.tools if t.name == "testfn__status").input_schema
    assert schema["properties"]["_display"]["enum"] == ["panel", "brief"]             # every tool offers the choice
    [row] = sql("SELECT args FROM task_runs WHERE id=$1", uuid.UUID(out["task_ids"][0]))
    assert json.loads(row["args"]) == {}                                              # never reaches the function
    opened = None
    deadline = time.time() + 5
    while time.time() < deadline and opened is None:
        m = sub.get_message(timeout=0.5)
        if m and m["type"] == "message":
            ev = json.loads(m["data"])
            if ev["type"] == "panel.open" and ev["task_id"] == out["task_ids"][0]:
                opened = ev
    assert opened and opened["data"]["display"] == "brief"


def test_a_panel_only_tool_always_opens_a_panel(stack, use_model):
    """A library or a quiz is something to look at: the tool says so, and "brief" is neither offered nor honoured."""
    fns = httpx.get(f"{stack['orch']}/registry/functions").json()
    fns = fns.get("functions", fns) if isinstance(fns, dict) else fns
    testfn = next(f for f in fns if f["name"] == "testfn")
    tools = [{**t, "display": "panel"} if t["name"] == "look" else t for t in testfn["tools"]]
    httpx.post(f"{stack['orch']}/registry/functions", json={**testfn, "tools": tools}).raise_for_status()
    try:
        sub = redis_sync.Redis.from_url(REDIS).pubsub()
        sub.subscribe("aurelia:events")
        model = use_model([{"calls": [("testfn__look", {"_display": "brief"})]}, {"text": "Here it is."}])
        out = httpx.post(f"{stack['orch']}/ask", json={"text": "what have I got?", "session_id": uuid.uuid4().hex},
                         timeout=30).json()
        schema = next(t for t in model.tools if t.name == "testfn__look").input_schema
        assert "_display" not in (schema.get("properties") or {})
        assert "_display" in next(t for t in model.tools if t.name == "testfn__status").input_schema["properties"]
        opened, deadline = None, time.time() + 5
        while time.time() < deadline and opened is None:
            m = sub.get_message(timeout=0.5)
            if m and m["type"] == "message":
                ev = json.loads(m["data"])
                if ev["type"] == "panel.open" and ev["task_id"] == out["task_ids"][0]:
                    opened = ev
        assert opened and opened["data"].get("display", "panel") == "panel"
    finally:
        httpx.post(f"{stack['orch']}/registry/functions", json=testfn).raise_for_status()


def add_task(tool, status="completed", minutes_ago=1, result=None, **kw):
    tid = uuid.uuid4()
    sql("INSERT INTO task_runs (id, tool, status, started_at, finished_at, user_id, result, progress_current, progress_total) "
        "VALUES ($1, $2, $3, now() - make_interval(mins => $4), CASE WHEN $3 = 'running' THEN NULL "
        "ELSE now() - make_interval(mins => $4) END, 'owner', $5::jsonb, $6, $7)",
        tid, tool, status, float(minutes_ago), json.dumps(result) if result is not None else None,
        kw.get("current"), kw.get("total"))
    if result is not None and status == "completed":
        sql("INSERT INTO panel_snapshots (task_id, tool, args, args_key, user_id, captured_at, panel) "
            "VALUES ($1, $2, '{}', '', 'owner', now() - make_interval(mins => $3), $4::jsonb)",
            tid, tool, float(minutes_ago), json.dumps(result))
    return tid


def test_glance_shows_only_what_is_off_or_moving(stack):
    sql("DELETE FROM panel_snapshots WHERE tool LIKE 'orbit.%'")
    add_task("orbit.status", minutes_ago=30, result={"devices": [
        {"name": "Front Door", "kind": "lock", "locked": False, "online": True},
        {"name": "Kitchen Leak", "kind": "leak_sensor", "active": True, "online": True},
        {"name": "Hall Motion", "kind": "motion_sensor", "active": False, "online": False},
        {"name": "Back Door", "kind": "lock", "locked": True, "online": True, "battery": 12},
        {"name": "Living Room Light", "kind": "light", "on": True, "online": True}]})
    add_task("orbit.lock", minutes_ago=5, result={"device": "Front Door", "locked": True})   # locked since
    add_task("nebula.download", status="running", current=64, total=100)
    sql("INSERT INTO tool_criticality (tool, level) VALUES ('orbit.lock', 'critical') ON CONFLICT (tool) DO NOTHING")
    add_task("orbit.lock", status="failed", minutes_ago=12)
    items = httpx.get(f"{stack['orch']}/glance").json()["items"]
    texts = [i.get("text") for i in items if i.get("text")]
    assert texts[0] == "Leak: Kitchen Leak"                                          # the worst first
    assert "Front Door unlocked" not in texts                                         # newer result says locked
    assert "Hall Motion offline" in texts and "Back Door battery 12%" in texts
    assert not any("Living Room Light" in t for t in texts)                           # normal things stay quiet
    live = [i for i in items if i["level"] == "live"]
    assert live and live[0]["tool"] == "nebula.download" and live[0]["percent"] == 64
    failed = [i for i in items if i.get("failed")]
    assert failed and failed[0]["tool"] == "orbit.lock"


def test_usual_follow_ups_come_from_his_own_requests(stack):
    for n in range(3):
        session = f"habit{n}{uuid.uuid4().hex[:8]}"
        tid = add_task("testfn.look", minutes_ago=100 - n)
        sql("INSERT INTO ask_requests (id, session_id, user_id, text, route, task_ids, created_at) VALUES "
            "($1, $2, 'owner', 'look it up', 'llm', $3::uuid[], now() - make_interval(mins => $4))",
            uuid.uuid4(), session, [tid], float(100 - n))
        follow = "turn off the living room light" if n < 2 else "something else"
        sql("INSERT INTO ask_requests (id, session_id, user_id, text, route, created_at) VALUES "
            "($1, $2, 'owner', $3, 'fastpath', now() - make_interval(mins => $4))", uuid.uuid4(), session, follow, float(99 - n))
    try:
        usual = httpx.get(f"{stack['orch']}/suggest", params={"tool": "testfn.look"}).json()["usual"]
        assert usual == [{"say": "turn off the living room light", "times": 2}]         # twice counts; once doesn't
        assert httpx.get(f"{stack['orch']}/suggest", params={"tool": "testfn.nothing"}).json()["usual"] == []
    finally:
        sql("DELETE FROM ask_requests WHERE session_id LIKE 'habit%'")                 # old requests: not for Mnemosyne
