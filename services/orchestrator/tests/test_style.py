"""How she speaks: the switch, the fixed lines, and the prompt."""
import asyncio
from pathlib import Path

import pytest

from app import style


@pytest.mark.parametrize("text,want", [
    ("use Raphael style", "raphael"), ("Try raphael mode", "raphael"), ("talk like Raphael", "raphael"),
    ("switch to Raphael's voice", "raphael"), ("please use the raphael style.", "raphael"),
    ("normal style", ""), ("go back to your normal style", ""), ("stop talking like Raphael", ""),
    ("turn off raphael mode", ""), ("back to your usual self", ""),
    ("who is Raphael", None), ("use the TV", None), ("raphael", None), ("style my hair", None),
])
def test_switch(text, want):
    assert style.switch(text) == want


class FakeRedis:
    def __init__(self): self.d = {}
    async def get(self, k): return self.d.get(k)
    async def set(self, k, v): self.d[k] = v.encode()
    async def delete(self, k): self.d.pop(k, None)


def test_choice_is_per_person_and_sticks():
    async def go():
        r = FakeRedis()
        assert await style.current(r, "owner") == ""
        said = await style.choose(r, "owner", "raphael")
        assert said.startswith("Answer.")
        assert await style.current(r, "owner") == "raphael"
        assert await style.current(r, "guest") == ""
        r.d[style.KEY.format("guest")] = b"something-else"      # unknown styles are ignored
        assert await style.current(r, "guest") == ""
        await style.choose(r, "owner", "")
        assert await style.current(r, "owner") == ""
    asyncio.run(go())


def test_fixed_lines():
    assert style.report("raphael", "The door is locked.") == "Report. The door is locked."
    assert style.report("raphael", "Notice. Already labeled.") == "Notice. Already labeled."
    assert style.report("", "The door is locked.") == "The door is locked."
    assert style.notice("raphael", "That didn't work: offline") == "Notice. That didn't work: offline"
    assert style.line("raphael", "declined", "Okay, I won't.").startswith("Understood.")
    assert style.line("", "declined", "Okay, I won't.") == "Okay, I won't."
    assert style.confirming("raphael", "That would place an order.", plain=False) == \
        "Notice. That would place an order. Do you wish to proceed?"
    assert style.confirming("raphael", "That needs your confirmation first.", plain=True).endswith("Do you wish to proceed?")
    assert style.confirming("", "x", plain=False) == "x"


def test_prompt(tmp_path, monkeypatch):
    (tmp_path / "styles").mkdir()
    (tmp_path / "styles" / "raphael.md").write_text("Speak precisely.\n")
    monkeypatch.setattr(style.settings, "config_path", str(tmp_path / "aurelia.yaml"))
    assert style.prompt("raphael") == "Speak precisely."
    assert style.prompt("") == ""
    assert style.prompt("../../etc/passwd") == ""


HERE = Path(__file__).resolve()
SHIPPED = HERE.parents[3] / "config" / "styles" / "raphael.md" if len(HERE.parents) > 3 else HERE.parent / "_none"


@pytest.mark.skipif(not SHIPPED.exists(), reason="the repo's config isn't in the test image")
def test_shipped_prompt_keeps_the_rules():
    text = SHIPPED.read_text()
    assert "never what you may do" in text and "Do you wish to proceed?" in text


def test_fast_path_replies_follow_the_style(monkeypatch):
    import os
    os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")
    os.environ.setdefault("REDIS_URL", "redis://localhost")
    from types import SimpleNamespace
    from app import assistant

    r = FakeRedis()
    app = SimpleNamespace(state=SimpleNamespace(redis=r))
    body = SimpleNamespace(user="owner")

    def plain(out):
        async def f(*a):
            return dict(out)
        return f

    async def run(out):
        monkeypatch.setattr(assistant, "_fastpath_plain", plain(out))
        return (await assistant._run_fastpath(app, body, None, None))["reply"]

    async def go():
        ok = {"reply": "Here's everything paired with me.", "needs_confirmation": [], "_ok": True}
        assert await run(ok) == "Here's everything paired with me."              # usual voice
        await style.choose(r, "owner", "raphael")
        assert await run(ok) == "Report. Here's everything paired with me."
        assert await run({"reply": "That didn't work: offline", "needs_confirmation": []}) == \
            "Notice. That didn't work: offline"
        held = await run({"reply": assistant.NEEDS_CONFIRMATION, "needs_confirmation": [{}]})
        assert held.endswith("Do you wish to proceed?")
        held = await run({"reply": "That would place an order.", "needs_confirmation": [{}]})
        assert held == "Notice. That would place an order. Do you wish to proceed?"
    asyncio.run(go())
