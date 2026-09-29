"""Mnemosyne end to end: real Postgres and Redis, the task contract, and a model double
that plays both Aurelia (chat) and the summarizer (summarize)."""
import asyncio
import json
import time
import uuid

import asyncpg
import httpx
import pytest

from .conftest import DB, REDIS

pytestmark = pytest.mark.skipif(not (DB and REDIS), reason="needs TEST_DATABASE_URL and TEST_REDIS_URL")


class Model:
    def __init__(self):
        self.chat, self.seen, self.broken = [], [], False

    def summary(self, transcript):
        """Summarizes by quoting: what Kentrell said, the last thing as the decision."""
        said = [l.split(": ", 1)[1] for l in transcript.splitlines() if "] Kentrell" in l]
        return json.dumps({"summary": "Talked about: " + " / ".join(said), "decisions": said[-1:],
                           "open": ["follow up"], "topics": ["frieren"] if "frieren" in transcript else ["misc"]})

    async def complete(self, route, system, messages, tools):
        from app.llm.base import Completion, ToolCall, Usage
        self.seen.append({"route": route, "system": system, "text": messages[-1].text})
        if route == "summarize":
            step = {"text": "not json" if self.broken else self.summary(messages[-1].text)}
        else:
            step = self.chat.pop(0) if self.chat else {"text": "Okay."}
        calls = [ToolCall(id=f"t{uuid.uuid4().hex[:8]}", name=c[0], args=c[1]) for c in step.get("calls", [])]
        return Completion(text=step.get("text", ""), tool_calls=calls, usage=Usage(10, 5), model="fake", provider="fake")


@pytest.fixture
def model(stack):
    from app.main import app
    original, fake = app.state.llm, Model()
    app.state.llm = fake
    yield fake
    app.state.llm = original


def sql(query, *args):
    async def go():
        conn = await asyncpg.connect(DB)
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()
    return asyncio.run(go())


def run_tool(stack, tool, args, confirmed=False):
    r = httpx.post(f"{stack['orch']}/tasks", json={"tool": tool, "args": args, "confirmed": confirmed})
    if r.status_code != 202:
        return r
    task = r.json()["task_id"]
    for _ in range(50):
        row = httpx.get(f"{stack['orch']}/tasks/{task}").json()
        if row["status"] != "running":
            row["result"] = json.loads(row["result"]) if isinstance(row["result"], str) else row["result"]
            return row
        time.sleep(0.1)
    raise AssertionError("tool didn't finish")


def wait_registered(stack):
    for _ in range(50):
        if any(f["name"] == "mnemosyne" for f in httpx.get(f"{stack['orch']}/registry/functions").json()):
            return
        time.sleep(0.1)
    raise AssertionError("Mnemosyne never registered")


def record(stack, quiet=0):
    return httpx.post(f"{stack['orch']}/mnemosyne/record", params={"quiet_minutes": quiet}, timeout=30).json()


def add_request(session, minutes_ago, text, route="llm", reply="ok", task_ids=()):
    sql("INSERT INTO ask_requests (id, session_id, user_id, text, route, reply, task_ids, created_at) "
        "VALUES ($1, $2, 'owner', $3, $4, $5, $6::uuid[], now() - make_interval(mins => $7))",
        uuid.uuid4(), session, text, route, reply, list(task_ids), float(minutes_ago))


def test_facts_are_kept_shown_given_to_her_and_removable(stack, model):
    wait_registered(stack)
    first = run_tool(stack, "mnemosyne.remember", {"fact": "Prefers the thermostat at 70°F at night"})
    assert first["status"] == "completed" and first["result"]["remembered_fact"].startswith("Prefers")
    again = run_tool(stack, "mnemosyne.remember", {"fact": "prefers the thermostat at 70°F at night"})
    assert again["result"]["message"] == "I already had that."
    run_tool(stack, "mnemosyne.remember", {"fact": "Guest wifi is called Solace"})
    run_tool(stack, "mnemosyne.remember", {"fact": "Watches anime on Fridays"})
    full = run_tool(stack, "mnemosyne.remember", {"fact": "A fourth thing"})       # max_facts: 3 in the test policy
    assert full["status"] == "failed" and "holding 3 facts" in full["error"]

    listed = run_tool(stack, "mnemosyne.facts", {})["result"]["facts"]
    assert [f["fact"] for f in listed] == ["Prefers the thermostat at 70°F at night", "Guest wifi is called Solace",
                                           "Watches anime on Fridays"]

    model.chat = [{"text": "Sure."}]
    httpx.post(f"{stack['orch']}/ask", json={"text": "hi", "session_id": uuid.uuid4().hex}, timeout=30)
    system = model.seen[-1]["system"]
    assert "Things Kentrell asked you to remember" in system and "- Guest wifi is called Solace" in system

    # The × on the facts panel, and forgetting (which always asks first).
    assert httpx.delete(f"{stack['orch']}/mnemosyne/facts/{listed[2]['id']}").is_success
    assert run_tool(stack, "mnemosyne.forget", {"fact_id": listed[1]["id"]}).status_code == 409
    gone = run_tool(stack, "mnemosyne.forget", {"fact_id": listed[1]["id"]}, confirmed=True)
    assert gone["result"]["forgot"] == "Guest wifi is called Solace"
    assert [f["fact"] for f in run_tool(stack, "mnemosyne.facts", {})["result"]["facts"]] == \
        ["Prefers the thermostat at 70°F at night"]


def test_quiet_conversations_become_records_found_by_meaning(stack, model):
    wait_registered(stack)
    session = uuid.uuid4().hex
    model.chat = [{"text": "Frieren is a fantasy anime about an elf mage."},
                  {"calls": [("testfn__switch", {})]}, {"text": "Done."},        # runs an excluded tool
                  {"text": "Friday works for the watch party."}]
    for text in ["what is frieren about", "flip the secret switch", "let's plan a frieren watch party friday"]:
        httpx.post(f"{stack['orch']}/ask", json={"text": text, "session_id": session}, timeout=30).raise_for_status()
    assert record(stack, quiet=30)["recorded"] == 0                # not quiet yet: nothing recorded
    assert record(stack)["recorded"] >= 1
    transcript = next(s["text"] for s in model.seen if s["route"] == "summarize" and "frieren" in s["text"])
    assert "what is frieren about" in transcript and "watch party" in transcript
    assert "secret switch" not in transcript                       # left out: it ran an excluded tool

    [rec] = sql("SELECT * FROM conversation_records WHERE session_id=$1", session)
    assert rec["requests"] == 3 and rec["model"] == "fake"
    assert json.loads(rec["decisions"]) == ["let's plan a frieren watch party friday"]
    assert rec["embedding"] is not None                            # embedded by the (fake) local model

    found = run_tool(stack, "mnemosyne.recall", {"query": "frieren watch party"})["result"]
    assert found["matched_by"] == "meaning" and found["recall"][0]["id"] == rec["id"]
    assert found["recall"][0]["open"] == ["follow up"] and found["recall"][0]["topics"] == ["frieren"]
    assert record(stack)["recorded"] == 0                          # already recorded: never twice


def test_stretches_split_at_gaps_and_the_active_one_waits(stack, model):
    session = uuid.uuid4().hex
    add_request(session, 180, "morning question about the garden")
    add_request(session, 175, "and the tomatoes")
    add_request(session, 1, "just now")                            # still going: not recorded yet
    record(stack, quiet=30)
    recs = sql("SELECT requests, summary FROM conversation_records WHERE session_id=$1", session)
    assert [(r["requests"], r["summary"]) for r in recs] == \
        [(2, "Talked about: morning question about the garden / and the tomatoes")]


def test_lone_commands_are_skipped_and_failures_fall_back_to_a_plain_list(stack, model):
    lone = uuid.uuid4().hex
    add_request(lone, 60, "lock the door", route="fastpath")
    stubborn = uuid.uuid4().hex
    add_request(stubborn, 60, "tell me about kepler")
    add_request(stubborn, 59, "and his laws")
    model.broken = True                                            # the "model" never returns usable JSON
    for _ in range(3):
        record(stack, quiet=30)
    [skip] = sql("SELECT skipped, summary FROM conversation_records WHERE session_id=$1", lone)
    assert skip["skipped"] and skip["summary"] is None
    [plain] = sql("SELECT model, summary FROM conversation_records WHERE session_id=$1", stubborn)
    assert plain["model"] == "none" and plain["summary"] == "Asked: tell me about kepler; and his laws"


def test_recall_by_day_and_forgetting_a_conversation(stack, model):
    session = uuid.uuid4().hex
    add_request(session, 90, "what's a good name for a cat")
    add_request(session, 88, "we'll go with Nova")
    record(stack, quiet=30)
    [rec] = sql("SELECT id, started_at FROM conversation_records WHERE session_id=$1", session)
    day = sql("SELECT to_char($1 AT TIME ZONE 'America/Chicago', 'YYYY-MM-DD') AS d", rec["started_at"])[0]["d"]
    that_day = run_tool(stack, "mnemosyne.recall", {"date": day, "limit": 20})["result"]
    assert rec["id"] in [r["id"] for r in that_day["recall"]] and that_day["date"]
    assert run_tool(stack, "mnemosyne.recall", {"date": "Tuesday"})["error"] == "Give the date as YYYY-MM-DD."

    out = run_tool(stack, "mnemosyne.forget", {"record_id": rec["id"]}, confirmed=True)
    assert out["result"]["forgot"].startswith("the conversation from")
    [left] = sql("SELECT skipped, summary, source_text, embedding FROM conversation_records WHERE id=$1", rec["id"])
    assert left["skipped"] and left["summary"] is None and left["source_text"] == "" and left["embedding"] is None
    assert rec["id"] not in [r["id"] for r in run_tool(stack, "mnemosyne.recall", {"date": day, "limit": 20})["result"]["recall"]]
    assert {r["text"] for r in sql("SELECT text, reply FROM ask_requests WHERE session_id=$1", session)} == {"(forgotten)"}
    assert record(stack, quiet=30)["recorded"] == 0                # forgotten words aren't recorded again
