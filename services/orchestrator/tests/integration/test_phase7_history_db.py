"""Her own history: releases keep whole commit messages; she writes her notes on each (what it did,
why, what she can do now), reads them back by release, function or date, knows her roadmap, and
carries her latest changes in every prompt. Real Postgres and Redis."""
import asyncio
import json
import time
import uuid
from pathlib import Path

import asyncpg
import httpx
import pytest

from .conftest import DB, REDIS

pytestmark = pytest.mark.skipif(not (DB and REDIS), reason="needs TEST_DATABASE_URL and TEST_REDIS_URL")

ROADMAP = """# Aurelia's roadmap
## 6. Kepler and context — Done
Builds: research.
Done when: cited answers.
## 7. Constellation — In progress
Builds: her computers.
Done when: open YouTube on the TV.
Built so far:
- Node health
- Verified sending
Still to do: power and presence.
## 8. Iris — Not started
Builds: sight.
Done when: she uses an app by seeing it.
"""


def sql(query, *args):
    async def go():
        conn = await asyncpg.connect(DB)
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()
    return asyncio.run(go())


class Model:
    """Writes release notes (route 'summarize') from the commits it's shown; answers chat plainly."""
    def __init__(self):
        self.notes_prompts, self.chat_systems = [], []

    async def complete(self, route, system, messages, tools):
        from app.llm.base import Completion, Usage
        if route == "summarize" and "release of your own software" in system:
            prompt = messages[-1].text
            self.notes_prompts.append(prompt)
            if "Send verified" in prompt:
                notes = {"summary": "Sending to another display is now checked on that display.",
                         "purpose": "Panels sent to a phone in the background were lost.",
                         "changes": ["Displays report their panels", "Unconfirmed sends are kept"],
                         "abilities": ["I can confirm a panel arrived before saying so"],
                         "functions": ["Constellation", "the display"], "for_him": ["Commit constellation.yaml"],
                         "phase": "7. Constellation"}
            else:
                notes = {"summary": "Kepler reads Word files from the file server.", "purpose": "He keeps documents there.",
                         "changes": ["Word files"], "abilities": ["I can read your Word documents"],
                         "functions": ["Kepler"], "for_him": [], "phase": "6. Kepler and context"}
            return Completion(text="Here you go:\n" + json.dumps(notes), tool_calls=[], usage=Usage(10, 5),
                              model="fake", provider="fake")
        self.chat_systems.append(system)
        return Completion(text="Okay.", tool_calls=[], usage=Usage(10, 5), model="fake", provider="fake")


def run_task(orch, tool, args, wait=30):
    tid = httpx.post(f"{orch}/tasks", json={"tool": tool, "args": args}).json()["task_id"]
    deadline = time.time() + wait
    while time.time() < deadline:
        row = httpx.get(f"{orch}/tasks/{tid}").json()
        if row["status"] != "running":
            res = row["result"]
            return row["status"], (json.loads(res) if isinstance(res, str) else res), row["error"]
        time.sleep(0.2)
    raise AssertionError("still running")


def release(sha, subject, body, paths, services, status="live"):
    commits = [{"sha": sha, "author": "kentrell@example.com", "subject": subject, "body": body}]
    [r] = sql("INSERT INTO releases (sha, prev_sha, author, message, changed_paths, services, plan, status, commits, "
              "finished_at) VALUES ($1, 'aaaa', 'k', $2, $3, $4, '{}'::jsonb, $5, $6::jsonb, now()) RETURNING id",
              sha, subject, paths, services, status, json.dumps(commits))
    return r["id"]


def test_she_knows_her_own_history_and_plan(stack):
    from app.main import app
    from app.settings import settings
    orch = stack["orch"]
    Path(settings.config_path).parent.joinpath("roadmap.md").write_text(ROADMAP)
    sql("DELETE FROM releases")
    asyncio.run(_flush_self())
    k = release("1111111aaaa", "Kepler: read documents from the file server",
                "read_pdf now reads PDFs, Word and text files from the file server.", ["services/functions/kepler/app/main.py"],
                ["kepler"])
    original, model = app.state.llm, Model()
    app.state.llm = model
    try:
        status, res, err = run_task(orch, "mnemosyne.history", {})
        assert status == "completed", err
        [e] = res["history"]
        assert e["summary"] == "Kepler reads Word files from the file server." and e["notes"]
        assert res["new_abilities"] == ["I can read your Word documents"]
        assert "read_pdf now reads PDFs, Word and text files" in model.notes_prompts[0]       # the body, not just the title
        assert "services/functions/kepler/app (1): main.py" in model.notes_prompts[0]
        assert "7. Constellation (In progress)" in model.notes_prompts[0]
        stored = sql("SELECT notes FROM releases WHERE id=$1", k)[0]["notes"]
        assert json.loads(stored)["purpose"] == "He keeps documents there."

        # A release goes live: Chrysalis reports it, and she writes her notes on it.
        c = release("2222222bbbb", "Phase 7: Send verified", "Displays report what they show; sends are confirmed.",
                    ["services/orchestrator/app/constellation.py", "config/governance/functions/constellation.yaml"],
                    ["orchestrator"])
        httpx.post(f"{orch}/releases/progress", json={"release_id": c, "sha": "2222222bbbb", "message": "Phase 7: Send verified",
                                                       "outcome": "live", "services": ["orchestrator"]}).raise_for_status()
        for _ in range(50):
            if sql("SELECT notes FROM releases WHERE id=$1", c)[0]["notes"]:
                break
            time.sleep(0.2)
        assert len(model.notes_prompts) == 2                                       # written once, not again
        status, res, err = run_task(orch, "mnemosyne.history", {"release": "latest"})
        r = res["history_release"]
        assert r["id"] == c and r["abilities"] == ["I can confirm a panel arrived before saying so"]
        assert r["for_him"] == ["Commit constellation.yaml"] and r["commit_messages"][0]["body"].startswith("Displays report")
        assert res["headline"].startswith(f"Release #{c}: Sending to another display")

        status, res, err = run_task(orch, "mnemosyne.history", {"function": "kepler"})
        assert [e["id"] for e in res["history"]] == [k]
        status, res, err = run_task(orch, "mnemosyne.history", {"since": "this week"})
        assert [e["id"] for e in res["history"]] == [c, k]
        status, res, err = run_task(orch, "mnemosyne.history", {"release": "nonsense words"})
        assert status == "failed"
        assert len(model.notes_prompts) == 2

        status, res, err = run_task(orch, "mnemosyne.roadmap", {})
        assert res["headline"] == "Phase 7 (Constellation) in progress · next: Iris"
        seven = next(p for p in res["roadmap"]["phases"] if p["n"] == 7)
        assert seven["built"] == ["Node health", "Verified sending"] and seven["todo"] == "power and presence."
        status, res, err = run_task(orch, "mnemosyne.roadmap", {"phase": "iris"})
        assert res["roadmap"]["phases"][0]["done_when"] == "she uses an app by seeing it."

        # Every request knows where she is and what changed lately.
        httpx.post(f"{orch}/ask", json={"text": "hi", "session_id": uuid.uuid4().hex}, timeout=30)
        system = model.chat_systems[-1]
        assert "phase 7 (Constellation) is in progress; next is 8 (Iris)" in system
        assert "Sending to another display is now checked on that display." in system
        assert system.index("Sending to another") < system.index("Kepler reads Word files")   # newest first
    finally:
        app.state.llm = original


async def _flush_self():
    import redis.asyncio as r
    c = r.Redis.from_url(REDIS)
    await c.delete("aurelia:self:context")
    await c.aclose()
