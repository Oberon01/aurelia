"""Lagrange's side of me, end to end: a connected service is governed like my own functions (no rules file:
every tool asks first; his own file can lower one), never takes one of my names, and runs by its own words;
the home's changes (Orbit, Home Assistant) and webhooks in start the automations waiting for them, once,
and only during quiet hours when they say so. Real Postgres and Redis; the services are stand-ins."""
import asyncio
import json
import threading
import time
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


def until(cond, wait=20, what="it"):
    deadline = time.time() + wait
    while time.time() < deadline:
        v = cond()
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError(f"{what} never happened")


@pytest.fixture(scope="module")
def svc(stack):
    """A stand-in for Orbit (status), a test function (act) and a connected service (gitea), all one server."""
    import uvicorn
    from fastapi import FastAPI
    from app.main import app
    from app.governance import ToolPolicy
    orch, port = stack["orch"], free_port()
    fn, seen = FastAPI(), {"runs": [], "devices": [{"name": "Front Door", "kind": "lock", "locked": True},
                                                   {"name": "Sink Leak", "kind": "leak_sensor", "active": False}]}

    async def work(tool: str, body: dict):
        seen["runs"].append((tool, body.get("args") or {}))
        data = ({"devices": seen["devices"]} if tool == "status" else
                {"output": {"connection": "Gitea", "tool": tool, "text": "aurelia\nmonarch"}, "headline": "aurelia"}
                if tool == "list_repos" else {"did": {"what": (body.get("args") or {}).get("what") or tool}})
        async with httpx.AsyncClient() as c:
            await c.post(f"{orch}/tasks/{body['task_id']}/result", json={"status": "completed", "data": data})

    @fn.post("/tools/{tool}/invoke", status_code=202)
    async def invoke(tool: str, body: dict):
        asyncio.create_task(work(tool, body))
        return {}

    @fn.post("/c/gitea/tools/{tool}/invoke", status_code=202)
    async def invoke_conn(tool: str, body: dict):
        asyncio.create_task(work(tool, body))
        return {}

    srv = uvicorn.Server(uvicorn.Config(fn, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    time.sleep(0.5)
    gov = Path(app.state.governance.root) / "functions"
    (gov / "lagfn.yaml").write_text("function: lagfn\nrate_limit_per_minute: 1000\ntools:\n  act: {tier: act}\n")
    app.state.governance.load_all()
    app.state.governance.functions["orbit"].tools["status"] = ToolPolicy(tier="reference")
    url = f"http://127.0.0.1:{port}"
    prop = lambda *ks: {"type": "object", "properties": {k: {"type": "string"} for k in ks}}

    def register():
        for name, tools in (("lagfn", [{"name": "act", "description": "act", "input_schema": prop("what")}]),
                            ("orbit", [{"name": "status", "description": "home", "input_schema": prop()}])):
            httpx.post(f"{orch}/registry/functions", json={"name": name, "description": "test", "url": url,
                                                           "tools": tools}).raise_for_status()
    seen.update(register=register, url=url, orch=orch, gov=gov)
    yield seen
    srv.should_exit = True
    (gov / "lagfn.yaml").unlink(missing_ok=True)


@pytest.fixture
def home(stack, svc):
    from app.main import app
    from app.config import FastPathPattern
    import yaml
    svc["register"]()
    real = yaml.safe_load(Path(__file__).parents[4].joinpath("config", "aurelia.yaml").read_text())["fastpath"]["patterns"]
    mine = [FastPathPattern(**p) for p in real if p["tool"].startswith(("pulsar.", "lagrange."))]
    mine += [FastPathPattern(name="t-act", match=r"^flip (?P<what>.+)$", tool="lagfn.act", args={"what": "{what}"})]
    cfg = app.state.config.current.fastpath
    old = cfg.patterns
    cfg.patterns = mine
    pol = app.state.governance.functions["pulsar"]
    old_rules = dict(pol.rules)
    sql("UPDATE jobs SET status='cancelled' WHERE created_by='owner' AND status IN ('scheduled','waiting','armed','paused')")
    asyncio.run(_flush(app))
    yield svc
    cfg.patterns = old
    pol.rules.clear()
    pol.rules.update(old_rules)


async def _flush(app):
    from redis.asyncio import Redis
    r = Redis.from_url(REDIS)
    async for k in r.scan_iter(match="aurelia:pulsar:state:*"):
        await r.delete(k)
    await r.aclose()


def ask(orch, text):
    return httpx.post(f"{orch}/ask", json={"text": text}, timeout=30).json()


def confirm(orch, out, approve=True):
    [a] = out["needs_confirmation"]
    return httpx.post(f"{orch}/ask/confirm", json={"action_id": a["action_id"], "approve": approve}, timeout=30).json()


def armed():
    return [dict(r) for r in sql("SELECT id, title, rule, status FROM jobs WHERE created_by='owner' AND status='armed' ORDER BY id")]


def state(orch, **body):
    return httpx.post(f"{orch}/pulsar/state", json={"source": "orbit", "kind": "lock", **body}, timeout=15)


def test_a_door_unlocking_starts_what_waits_for_it_once(home):
    from app.main import app
    orch = home["orch"]
    out = ask(orch, "when the front door unlocks during quiet hours, flip the hall light")
    assert out["route"] == "fastpath" and out["reply"].startswith("When Front Door unlocks during quiet hours: Flip the hall light."), out
    confirm(orch, out)
    out = ask(orch, "flip the porch light when the front door unlocks")
    assert out["reply"].startswith("When Front Door unlocks: Flip the porch light."), out
    confirm(orch, out)
    rules = [json.loads(j["rule"]) if isinstance(j["rule"], str) else j["rule"] for j in armed()]
    assert [(r["on"], r["source"], r["entity"], r["to"], r.get("only")) for r in rules] == [
        ("state", "orbit", "front door", "unlocked", "quiet"), ("state", "orbit", "front door", "unlocked", None)]

    app.state.governance.functions["pulsar"].rules["quiet_hours"] = ""            # not quiet now
    assert state(orch, id="front door", name="Front Door", state={"locked": True}).json() == {"event": None}  # first look
    r = state(orch, id="front door", name="Front Door", state={"locked": False}).json()
    assert r == {"event": "unlocked", "started": 1}                                # the quiet-hours one waits
    assert state(orch, id="front door", name="Front Door", state={"locked": False}).json() == {"event": None}  # said twice
    until(lambda: [x for x in home["runs"] if x == ("act", {"what": "the porch light"})], what="the porch light")
    assert not [x for x in home["runs"] if x == ("act", {"what": "the hall light"})]

    until(lambda: len(armed()) == 2, what="the porch light's automation waiting again")
    app.state.governance.functions["pulsar"].rules["quiet_hours"] = "00:00-23:59"   # quiet now
    sql("UPDATE jobs SET last_run_at = now() - interval '1 hour' WHERE created_by='owner'")
    state(orch, id="front door", name="Front Door", state={"locked": True})
    r = state(orch, id="front door", name="Front Door", state={"locked": False}).json()
    assert r["started"] == 2
    until(lambda: [x for x in home["runs"] if x == ("act", {"what": "the hall light"})], what="the hall light")

    bad = httpx.post(f"{orch}/pulsar/state", json={"source": "orbit", "id": "front door", "kind": "lock",
                                                    "state": {"locked": True}}, headers={"x-aurelia-device": "1"})
    assert bad.status_code == 403                                                  # never from a display


def test_home_assistant_says_what_it_was(home):
    orch = home["orch"]
    r = httpx.post(f"{orch}/pulsar/state", json={"source": "ha:home_assistant", "id": "person.kentrell", "kind": "person",
                                                  "state": {"state": "home"}, "was": "not_home"}).json()
    assert r["event"] == "home"                                                     # a first sighting, with its "was"
    r = httpx.post(f"{orch}/pulsar/state", json={"source": "ha:home_assistant", "id": "binary_sensor.back", "kind": "door",
                                                  "state": {"state": "unavailable"}}).json()
    assert r == {"event": None}


def test_a_webhook_in_starts_what_waits_for_it(home):
    orch = home["orch"]
    sql("DELETE FROM connections WHERE name='arrive_home'")
    sql("INSERT INTO connections (name, label, kind, status) VALUES ('arrive_home', 'Arrive home', 'hook_in', 'connected')")
    out = ask(orch, "when the arrive home webhook is called, flip the porch light")
    assert out["reply"].startswith("When the Arrive home webhook is called: Flip the porch light."), out
    confirm(orch, out)
    r = httpx.post(f"{orch}/pulsar/hook", json={"hook": "arrive_home", "label": "Arrive home", "data": {"x": 1}}).json()
    assert r == {"started": 1}
    out = ask(orch, "when the nonexistent webhook is called, flip x")
    assert out.get("needs_confirmation") == [] or out["route"] != "fastpath"        # not something I can wait for
    sql("DELETE FROM connections WHERE name='arrive_home'")


def test_a_connected_service_asks_first_until_his_own_rules_say_otherwise(home):
    from app.main import app
    from app import assistant
    orch, url = home["orch"], home["url"]
    reg = {"name": "gitea", "via": "lagrange", "description": "Gitea", "url": f"{url}/c/gitea",
           "tools": [{"name": "list_repos", "description": "List repositories.", "input_schema": {"type": "object", "properties": {}}},
                     {"name": "create_issue", "description": "Open an issue.",
                      "input_schema": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}}]}
    assert httpx.post(f"{orch}/registry/functions", json=reg).status_code == 204
    for taken in ("orbit", "lagfn"):                                   # never one of my own functions' names
        r = httpx.post(f"{orch}/registry/functions", json={**reg, "name": taken})
        assert r.status_code == 409 and "my own" in r.json()["detail"]
    httpx.delete(f"{orch}/registry/functions/orbit", params={"via": "lagrange"})   # Lagrange can't take mine away
    assert any(f["name"] == "orbit" for f in httpx.get(f"{orch}/registry/functions").json())

    out = ask(orch, "gitea list repos")
    assert out["route"] == "fastpath" and out["needs_confirmation"], out            # first use asks you
    assert not [x for x in home["runs"] if x[0] == "list_repos"]
    done = confirm(orch, out)
    until(lambda: [x for x in home["runs"] if x[0] == "list_repos"], what="list_repos")
    assert done

    async def defs_for():
        from types import SimpleNamespace
        from redis.asyncio import Redis
        r = Redis.from_url(REDIS)
        try:
            return await assistant._tool_defs(SimpleNamespace(state=SimpleNamespace(redis=r, governance=app.state.governance)),
                                              "owner", only="gitea")
        finally:
            await r.aclose()
    defs, by_api, fns = asyncio.run(defs_for())
    assert sorted(by_api.values()) == ["gitea.create_issue", "gitea.list_repos"] and [f["name"] for f in fns] == ["gitea"]
    assert all("Requires the user's confirmation" in d.description for d in defs)

    (home["gov"] / "gitea.yaml").write_text("function: gitea\nconnection: true\ntools:\n  list_repos: {tier: reference}\n")
    app.state.governance.load_all()
    try:
        out = ask(orch, "gitea, list my repos")
        assert out["route"] == "fastpath" and not out["needs_confirmation"], out
        out = ask(orch, "gitea create issue")                                       # needs a title: not by its name
        assert out["route"] != "fastpath" or out.get("needs_confirmation") is not None
    finally:
        (home["gov"] / "gitea.yaml").unlink()
        app.state.governance.load_all()
        app.state.governance.functions.pop("gitea", None)
        httpx.delete(f"{orch}/registry/functions/gitea")
