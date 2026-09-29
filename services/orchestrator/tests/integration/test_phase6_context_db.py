"""Phase 6 context, end to end: real Postgres and Redis, the fake function, and a
model double that records what it's shown and answers from a script."""
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


class Model:
    """Stands in for Claude: each call pops the next step and records the messages it saw."""
    def __init__(self):
        self.steps, self.seen = [], []

    async def complete(self, route, system, messages, tools):
        from app.llm.base import Completion, ToolCall, Usage
        self.seen.append({"messages": list(messages), "tools": [t.name for t in tools]})
        step = self.steps.pop(0) if self.steps else {"text": "Okay."}
        if "delay" in step:
            await asyncio.sleep(step["delay"])
        calls = [ToolCall(id=f"t{uuid.uuid4().hex[:8]}", name=c[0], args=c[1]) for c in step.get("calls", [])]
        return Completion(text=step.get("text", ""), tool_calls=calls, usage=Usage(10, 5), model="fake", provider="fake")


@pytest.fixture
def model(stack):
    from app.main import app
    original, fake = app.state.llm, Model()
    app.state.llm = fake
    yield fake
    app.state.llm = original


def ask(stack, text, session, screen=None):
    body = {"text": text, "session_id": session}
    if screen is not None:
        body["screen"] = screen
    r = httpx.post(f"{stack['orch']}/ask", json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def last_user(seen):
    return seen["messages"][-1].text


def wait_done(stack, task_id):
    for _ in range(50):
        if httpx.get(f"{stack['orch']}/tasks/{task_id}").json()["status"] != "running":
            return
        time.sleep(0.1)


def test_follow_ups_see_the_screen_and_what_came_before(stack, model):
    session = uuid.uuid4().hex
    model.steps = [{"calls": [("testfn__status", {})]}, {"text": "Here's the home."}]
    first = ask(stack, "what's the status of my home", session)
    [task] = first["task_ids"]
    wait_done(stack, task)

    # "the third one" with the home panel on screen as panel 1, focused.
    screen = {"panels": [{"n": 1, "task_id": task, "tool": "testfn.status", "title": "Home"}], "focus": 1}
    model.steps = [{"text": "That's the bedroom lamp."}]
    ask(stack, "what's the third one", session, screen)
    msg = last_user(model.seen[-1])
    assert msg.startswith("<context>") and msg.endswith("what's the third one")
    assert "Panel 1 · Home · testfn.status [FOCUSED]" in msg
    assert '{"#":3,"name":"Bedroom Lamp"' in msg
    assert "display__panel" in model.seen[-1]["tools"]
    history = [(m.role, m.text) for m in model.seen[-1]["messages"][:-1]]
    assert history == [("user", "what's the status of my home"), ("assistant", "Here's the home.")]

    # Panel closed: the result is still there, as an earlier result, and the focused turn is labelled.
    model.steps = [{"text": "Yes."}]
    ask(stack, "was the lamp off?", session, {"panels": [], "focus": None})
    seen = model.seen[-1]
    assert "Earlier results in this conversation" in last_user(seen) and "Bedroom Lamp" in last_user(seen)
    assert seen["messages"][2].text == "[about panel 1 · Home] what's the third one"
    assert "display__panel" not in seen["tools"]                  # nothing on screen to read

    async def screen_saved():
        conn = await asyncpg.connect(DB)
        try:
            return await conn.fetchval("SELECT screen FROM ask_requests WHERE text=$1", "what's the third one")
        finally:
            await conn.close()
    saved = json.loads(asyncio.run(screen_saved()))
    assert saved["focus"] == 1 and saved["panels"][0]["task_id"] == task


def test_reading_a_whole_panel(stack, model):
    session = uuid.uuid4().hex
    model.steps = [{"calls": [("testfn__status", {})]}, {"text": "Done."}]
    [task] = ask(stack, "home status", session)["task_ids"]
    wait_done(stack, task)
    screen = {"panels": [{"n": 3, "task_id": task, "tool": "testfn.status", "title": "Home"}], "focus": None}
    model.steps = [{"calls": [("display__panel", {"panel": 3}), ("display__panel", {"panel": 7})]}, {"text": "All of it."}]
    out = ask(stack, "read me everything on panel 3", session, screen)
    assert out["reply"] == "All of it." and out["task_ids"] == []            # reading a panel runs no task
    tool_msgs = [m for m in model.seen[-1]["messages"] if m.role == "tool"]
    whole, missing = tool_msgs
    assert '"panel": 3' in whole.text and "Living Room TV" in whole.text and not whole.is_error
    assert "there is no panel 7 on screen" in missing.text and missing.is_error


def test_two_requests_at_once_are_both_remembered(stack, model):
    session = uuid.uuid4().hex
    model.steps = [{"text": "First.", "delay": 0.5}, {"text": "Second."}]

    async def both():
        async with httpx.AsyncClient(timeout=30) as c:
            return await asyncio.gather(
                c.post(f"{stack['orch']}/ask", json={"text": "one", "session_id": session}),
                c.post(f"{stack['orch']}/ask", json={"text": "two", "session_id": session}))
    replies = asyncio.run(both())
    assert all(r.is_success and r.json()["session_id"] == session for r in replies)
    stored = [json.loads(x) for x in redis_sync.Redis.from_url(REDIS).lrange(f"aurelia:conv:{session}", 0, -1)]
    assert sorted(t["text"] for t in stored if t["role"] == "user") == ["one", "two"]
    assert sorted(t["text"] for t in stored if t["role"] == "assistant") == ["First.", "Second."]
    ttl = redis_sync.Redis.from_url(REDIS).ttl(f"aurelia:conv:{session}")
    assert ttl > 23 * 3600                                          # a day, not an hour

    model.steps = [{"text": "Both."}]
    ask(stack, "and three", session)
    roles = [m.role for m in model.seen[-1]["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "user"]   # each turn stored whole: never interleaved


def test_the_conversation_comes_back_after_a_reload(stack, model):
    session = uuid.uuid4().hex
    model.steps = [{"text": "Hi there."}]
    ask(stack, "hello", session, {"panels": [{"n": 2, "task_id": str(uuid.uuid4()), "tool": "t.x", "title": "Page"}], "focus": 2})
    turns = httpx.get(f"{stack['orch']}/ask/history", params={"session_id": session}).json()["turns"]
    assert turns == [{"role": "user", "text": "hello", "about": "2 · Page"},
                     {"role": "assistant", "text": "Hi there.", "about": None}]
    assert httpx.get(f"{stack['orch']}/ask/history", params={"session_id": "../x"}).json() == {"turns": []}


def test_a_made_up_session_id_is_replaced(stack, model):
    out = ask(stack, "hello", "../../etc/passwd")
    assert out["session_id"] != "../../etc/passwd" and len(out["session_id"]) == 32
