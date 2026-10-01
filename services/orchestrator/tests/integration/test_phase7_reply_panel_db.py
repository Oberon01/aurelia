"""A long reply opens as a panel on the display that asked; the top line keeps its first sentence.
A turn that already opened a panel keeps its reply in the top line. Real Postgres and Redis."""
import json
import time
import uuid

import httpx
import pytest
import redis as redis_sync

from .conftest import DB, REDIS

pytestmark = pytest.mark.skipif(not (DB and REDIS), reason="needs TEST_DATABASE_URL and TEST_REDIS_URL")

LONG = ("Your desktop's GPU has 8 GB, which is tight for local vision models.\n\n"
        "A 16 GB card runs them comfortably and leaves room for a voice model too. "
        "The RTX 4060 Ti 16 GB is the cheapest way there; the 4070 Ti Super is faster. "
        "Either fits the 650 W power supply you planned.")


class Model:
    def __init__(self, steps):
        self.steps = steps

    async def complete(self, route, system, messages, tools):
        from app.llm.base import Completion, ToolCall, Usage
        step = self.steps.pop(0) if self.steps else {"text": "Okay."}
        calls = [ToolCall(id=f"t{uuid.uuid4().hex[:8]}", name=c[0], args=c[1]) for c in step.get("calls", [])]
        return Completion(text=step.get("text", ""), tool_calls=calls, usage=Usage(10, 5), model="fake", provider="fake")


def events_for(sub, seconds=3):
    out, end = [], time.time() + seconds
    while time.time() < end:
        m = sub.get_message(timeout=0.3)
        if m and m["type"] == "message":
            out.append(json.loads(m["data"]))
    return out


def test_long_replies_open_as_a_panel(stack):
    from app.main import app
    orch = stack["orch"]
    httpx.post(f"{orch}/registry/functions", json={
        "name": "testfn", "description": "test", "url": stack["fn"],
        "tools": [{"name": n, "description": n, "input_schema": {"type": "object"}} for n in ("status", "look")]
    }).raise_for_status()
    sub = redis_sync.Redis.from_url(REDIS).pubsub()
    sub.subscribe("aurelia:events")
    original = app.state.llm
    session = uuid.uuid4().hex
    try:
        app.state.llm = Model([{"text": LONG}])
        out = httpx.post(f"{orch}/ask", json={"text": "is my desktop's GPU enough?", "session_id": session},
                         timeout=30).json()
        evs = events_for(sub)
        app.state.llm = Model([{"text": "Yes."}])
        short = httpx.post(f"{orch}/ask", json={"text": "and the lights?", "session_id": session}, timeout=30).json()
        app.state.llm = Model([{"calls": [("testfn__status", {})]}, {"text": LONG}])
        with_panel = httpx.post(f"{orch}/ask", json={"text": "how's the house", "session_id": session}, timeout=30).json()
    finally:
        app.state.llm = original

    assert out["reply"] == LONG and out["reply_short"] == "Your desktop's GPU has 8 GB, which is tight for local vision models."
    tid = out["reply_panel"]
    mine = [e for e in evs if e["task_id"] == tid]
    assert [e["type"] for e in mine] == ["panel.open", "panel.result", "panel.close"]
    assert mine[0]["tool"] == "aurelia.reply" and mine[0]["data"]["tier"] == "said"
    assert mine[1]["data"]["answer"] == LONG and mine[1]["data"]["asked"] == "is my desktop's GPU enough?"
    row = httpx.get(f"{orch}/tasks/{tid}").json()
    assert row["status"] == "completed" and row["trigger"] == "reply"
    assert "reply_panel" not in short
    assert "reply_panel" not in with_panel and with_panel["reply"] == LONG      # its panel is already up
    hist = httpx.get(f"{orch}/ask/history", params={"session_id": session}).json()["turns"]
    assert hist[1]["text"] == LONG                                              # she remembers all of it
