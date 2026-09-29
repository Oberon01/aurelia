"""What the orchestrator gives Kepler: model calls (budgeted and logged), embeddings,
and quiz results that can become part of his profile. Real Postgres and Redis."""
import asyncio
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


class Model:
    def __init__(self):
        self.calls = []

    async def complete(self, route, system, messages, tools):
        from app.llm.base import Completion, Usage
        self.calls.append((route, system, [m.text for m in messages], tools))
        return Completion(text='{"ok": true}', tool_calls=[], usage=Usage(1200, 300), model="fake-research", provider="fake")


@pytest.fixture
def model(stack):
    from app.main import app
    original, fake = app.state.llm, Model()
    app.state.llm = fake
    yield fake
    app.state.llm = original


def test_functions_get_models_through_the_orchestrator(stack, model):
    before = sql("SELECT count(*) AS n FROM llm_calls WHERE model='fake-research'")[0]["n"]
    r = httpx.post(f"{stack['orch']}/llm/complete", json={"purpose": "research", "system": "You plan.",
                                                           "messages": [{"role": "user", "text": "q"}], "function": "kepler"})
    assert r.json()["text"] == '{"ok": true}' and model.calls[-1][:3] == ("research", "You plan.", ["q"])
    assert model.calls[-1][3] == []                                               # no tools: text only
    assert sql("SELECT count(*) AS n FROM llm_calls WHERE model='fake-research'")[0]["n"] == before + 1
    assert httpx.post(f"{stack['orch']}/llm/complete", json={"purpose": "anything", "messages": [{"role": "user", "text": "x"}]}).status_code == 400
    assert httpx.post(f"{stack['orch']}/llm/complete", json={"purpose": "research", "messages": [{"role": "tool", "text": "x"}]}).status_code == 400
    vecs = httpx.post(f"{stack['orch']}/llm/embed", json={"texts": ["azure landing zones"]}).json()["embeddings"]
    assert len(vecs) == 1 and len(vecs[0]) == 768


def test_a_strong_quiz_becomes_a_learning_proposal(stack):
    weak = httpx.post(f"{stack['orch']}/learning/quiz", json={"subject": "BGP", "score": 2, "total": 5}).json()
    strong = httpx.post(f"{stack['orch']}/learning/quiz", json={"subject": "Azure landing zones", "score": 5, "total": 5,
                                                                 "missed": []}).json()
    try:
        assert weak == {"recorded": True, "proposed": False} and strong == {"recorded": True, "proposed": True}
        [item] = [i for i in httpx.get(f"{stack['orch']}/mailbox").json()["items"] if i["title"].startswith("Solid on Azure")]
        assert item["kind"] == "about_you" and item["proposal"]["category"] == "learning" and not item["proposal"]["core"]
        assert len(sql("SELECT 1 FROM quiz_results")) == 2
        assert httpx.post(f"{stack['orch']}/learning/quiz", json={"subject": "x", "score": 6, "total": 5}).status_code == 400
    finally:
        sql("DELETE FROM mailbox_items WHERE source='kepler'")                   # not part of later tests' mailbox
