"""Each display keeps its own panels: a panel opens only on the display that asked for it.
Panels nobody in particular asked for go to every display except node screens, and the
mailbox count goes everywhere. Real Postgres, Redis and WebSockets (the proxy's header is set
by hand here, as Traefik sets it after checking the key)."""
import asyncio
import json
import time
import uuid

import asyncpg
import httpx
import pytest

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


def device(kind, name):
    [row] = sql("INSERT INTO devices (kind, name, status, key_hash, approved_at) VALUES ($1, $2, 'approved', $3, now()) "
                "RETURNING id", kind, name, uuid.uuid4().hex)
    return row["id"]


class Model:
    def __init__(self, steps):
        self.steps = steps

    async def complete(self, route, system, messages, tools):
        from app.llm.base import Completion, ToolCall, Usage
        step = self.steps.pop(0) if self.steps else {"text": "Okay."}
        calls = [ToolCall(id=f"t{uuid.uuid4().hex[:8]}", name=c[0], args=c[1]) for c in step.get("calls", [])]
        return Completion(text=step.get("text", ""), tool_calls=calls, usage=Usage(10, 5), model="fake", provider="fake")


async def listen(url, dev, seconds, out):
    """Collects what one display receives for `seconds`; records it if the server closes it."""
    import websockets
    headers = {"X-Aurelia-Device": str(dev)} if dev is not None else {}
    try:
        async with websockets.connect(url, additional_headers=headers) as ws:
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                try:
                    out.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=0.3)))
                except asyncio.TimeoutError:
                    pass
    except websockets.ConnectionClosed as exc:
        out.append({"closed": exc.rcvd.code if exc.rcvd else None})


def tasks_seen(events, kind="panel.open"):
    return {e["task_id"] for e in events if e.get("type") == kind}


def test_each_display_keeps_its_own_panels(stack):
    from app.main import app
    orch, ws_url = stack["orch"], stack["orch"].replace("http", "ws") + "/ws"
    desk, phone, tv, term = (device("browser", "Desk"), device("browser", "Phone"),
                             device("screen", "Living room TV screen"), device("terminal", "ask.sh"))
    got = {k: [] for k in ("desk", "phone", "tv", "inside")}
    original = app.state.llm
    httpx.post(f"{orch}/registry/functions", json={              # registrations lapse in a long test run
        "name": "testfn", "description": "test", "url": stack["fn"],
        "tools": [{"name": n, "description": n, "input_schema": {"type": "object"}} for n in ("status", "look")]
    }).raise_for_status()

    async def scenario():
        listeners = [asyncio.create_task(listen(ws_url, d, 6, got[k]))
                     for k, d in (("desk", desk), ("phone", phone), ("tv", tv), ("inside", None))]
        await asyncio.sleep(0.8)
        async with httpx.AsyncClient(timeout=30) as c:
            app.state.llm = Model([{"calls": [("testfn__status", {})]}, {"text": "All quiet."}])
            asked = (await c.post(f"{orch}/ask", json={"text": "how's the house", "session_id": uuid.uuid4().hex},
                                  headers={"X-Aurelia-Device": str(phone)})).json()
            # The display re-opening a panel after a reload: still its own.
            reopened = (await c.post(f"{orch}/tasks", json={"tool": "testfn.look", "args": {}},
                                     headers={"X-Aurelia-Device": str(desk)})).json()
            assert "task_id" in reopened, reopened
            reopened = reopened["task_id"]
            # A client can't pick someone else's display.
            forged = (await c.post(f"{orch}/tasks", json={"tool": "testfn.look", "args": {"x": 1}, "device": tv},
                                   headers={"X-Aurelia-Device": str(desk)})).json()["task_id"]
            # Nobody in particular: a schedule (from inside), or the terminal.
            app.state.llm = Model([{"calls": [("testfn__look", {"y": 2})]}, {"text": "Done."}])
            from_term = (await c.post(f"{orch}/ask", json={"text": "look", "session_id": uuid.uuid4().hex},
                                      headers={"X-Aurelia-Device": str(term)})).json()
            scheduled = (await c.post(f"{orch}/tasks", json={"tool": "testfn.status", "args": {"z": 3},
                                                              "trigger": "pulsar"})).json()["task_id"]
            import redis as redis_sync                               # the "N for review" chip
            from app.events import CHANNEL
            redis_sync.Redis.from_url(REDIS).publish(CHANNEL, json.dumps(
                {"type": "mailbox.changed", "task_id": "mailbox", "tool": "mailbox", "data": {"waiting": 1}}))
            await asyncio.sleep(0.5)
        await asyncio.gather(*listeners)
        return asked["task_ids"][0], reopened, forged, from_term["task_ids"][0], scheduled

    try:
        mine, reopened, forged, from_term, scheduled = asyncio.run(scenario())
    finally:
        app.state.llm = original

    opened = {k: tasks_seen(v) for k, v in got.items()}
    assert mine in opened["phone"] and mine not in opened["desk"] and mine not in opened["tv"]
    assert {e["type"] for e in got["phone"] if e.get("task_id") == mine} >= {"panel.open", "panel.result", "panel.close"}
    assert not [e for e in got["desk"] + got["tv"] if e.get("task_id") == mine]       # no progress or results either
    assert reopened in opened["desk"] and reopened not in opened["phone"]
    assert forged in opened["desk"] and forged not in opened["tv"]
    for unaddressed in (from_term, scheduled):
        assert unaddressed in opened["desk"] and unaddressed in opened["phone"] and unaddressed not in opened["tv"]
    assert opened["inside"] >= {mine, reopened, forged, from_term, scheduled}          # inside the stack: everything
    for k in ("desk", "phone", "tv", "inside"):
        assert any(e.get("type") == "mailbox.changed" for e in got[k]), k
    [row] = sql("SELECT device FROM task_runs WHERE id=$1", uuid.UUID(mine))
    assert row["device"] == phone


def test_a_revoked_display_is_disconnected(stack):
    orch, ws_url = stack["orch"], stack["orch"].replace("http", "ws") + "/ws"
    dev = device("browser", "Old tablet")
    got = []

    async def scenario():
        listener = asyncio.create_task(listen(ws_url, dev, 15, got))
        await asyncio.sleep(0.8)
        async with httpx.AsyncClient() as c:
            (await c.post(f"{orch}/devices/{dev}/revoke")).raise_for_status()
        started = time.monotonic()
        await listener
        return time.monotonic() - started

    took = asyncio.run(scenario())
    assert got and got[-1] == {"closed": 4401} and took < 10
