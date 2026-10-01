"""Raphael style, end to end: switching it on and off by saying so, the model told how to
speak, and the fixed lines (a held action declined) in her new voice. Real Postgres and Redis."""
import uuid
from pathlib import Path

import httpx
import pytest

from .conftest import DB, REDIS

pytestmark = pytest.mark.skipif(not (DB and REDIS), reason="needs TEST_DATABASE_URL and TEST_REDIS_URL")


class Model:
    def __init__(self, steps):
        self.steps, self.systems = steps, []

    async def complete(self, route, system, messages, tools):
        from app.llm.base import Completion, ToolCall, Usage
        self.systems.append(system)
        step = self.steps.pop(0) if self.steps else {"text": "Okay."}
        calls = [ToolCall(id=f"t{uuid.uuid4().hex[:8]}", name=c[0], args=c[1]) for c in step.get("calls", [])]
        return Completion(text=step.get("text", ""), tool_calls=calls, usage=Usage(10, 5), model="fake", provider="fake")


@pytest.fixture
def model(stack):
    from app.main import app
    from app.settings import settings
    styles = Path(settings.config_path).parent / "styles"
    styles.mkdir(exist_ok=True)
    (styles / "raphael.md").write_text("Speaking style: Raphael. Open with Answer., Report. or Notice.\n")
    original = app.state.llm
    app.state.llm = Model([])
    yield app.state.llm
    app.state.llm = original


def ask(stack, text, session):
    return httpx.post(f"{stack['orch']}/ask", json={"text": text, "session_id": session}, timeout=30).json()


def test_raphael_style_on_and_off(stack, model):
    s = uuid.uuid4().hex
    on = ask(stack, "use Raphael style", s)
    assert on["route"] == "style" and on["reply"].startswith("Answer.") and not on["task_ids"]
    assert model.systems == []                                    # switching never reaches the model

    model.steps = [{"text": "Answer. Yes."}]
    ask(stack, "are you there?", s)
    assert model.systems[-1].rstrip().endswith("Open with Answer., Report. or Notice.")

    # A held action, declined: her fixed line is in the new style too.
    model.steps = [{"calls": [("mnemosyne__forget", {"ids": [1]})]}, {"text": "Notice. That needs your confirmation."}]
    held = ask(stack, "forget fact 1", s)
    [action] = held["needs_confirmation"]
    no = httpx.post(f"{stack['orch']}/ask/confirm", json={"action_id": action["action_id"], "approve": False}).json()
    assert no["reply"] == "Understood. The action has been cancelled."

    off = ask(stack, "normal style", s)
    assert off["route"] == "style" and off["reply"] == "Okay, I'm back to my usual self."
    model.steps = [{"text": "Yep, I'm here."}]
    ask(stack, "are you there?", s)
    assert "Speaking style: Raphael" not in model.systems[-1]
    model.steps = [{"calls": [("mnemosyne__forget", {"ids": [1]})]}]
    held = ask(stack, "forget fact 1", s)
    [action] = held["needs_confirmation"]
    no = httpx.post(f"{stack['orch']}/ask/confirm", json={"action_id": action["action_id"], "approve": False}).json()
    assert no["reply"] == "Okay, I won't."
