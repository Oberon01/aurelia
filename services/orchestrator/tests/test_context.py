"""Conversation context without a database: short forms, the screen block, stored turns."""
import asyncio
import json

from app import context
from app.context import PanelRef, Screen

SEARCH = {"query": "frieren", "results": [
    {"title": f"Result {i}", "url": f"https://example.org/{i}", "snippet": "x" * 900, "thumbnail": "https://i/img.jpg"}
    for i in range(1, 26)]}


def test_short_form_numbers_items_in_panel_order_and_drops_images():
    out = json.loads(context.compact(SEARCH, 100_000).replace("…(cut; read the panel for all of it)", ""))
    items = out["results"]
    assert items[0]["#"] == 1 and items[2] == {"#": 3, "title": "Result 3", "url": "https://example.org/3",
                                                "snippet": "x" * 300 + "…"}
    assert items[-1] == "(+5 more)" and len(items) == 21          # 20 shown, the rest counted
    assert "thumbnail" not in items[0]


def test_short_form_is_cut_to_its_budget():
    text = context.compact(SEARCH, 500)
    assert len(text) < 560 and text.endswith("read the panel for all of it)")


def fake_results(found):
    async def results(ids):
        return {i: found[i] for i in ids if i in found}
    return results


def done(tool, result):
    return {"tool": tool, "status": "completed", "result": result, "error": None, "progress": (None, None, None)}


def test_screen_block_marks_focus_and_keeps_earlier_results(monkeypatch):
    found = {"a": done("nebula.search", SEARCH),
             "b": {"tool": "nebula.download", "status": "running", "result": None, "error": None,
                   "progress": (3.0, 10.0, "bytes")},
             "old": done("nebula.youtube", {"videos": [{"title": "Frieren OST", "url": "https://youtu.be/1"}]})}
    monkeypatch.setattr(context, "results", fake_results(found))
    screen = Screen(panels=[PanelRef(n=2, task_id="b", tool="nebula.download", title="Download"),
                            PanelRef(n=1, task_id="a", tool="nebula.search", title="Web search")], focus=1)
    history = [{"role": "user", "text": "frieren ost on youtube", "tasks": ["old"]},
               {"role": "assistant", "text": "Here are some videos."},
               {"role": "user", "text": "search frieren", "tasks": ["a"]},        # on screen: not repeated
               {"role": "assistant", "text": "Found 25."}]
    text = asyncio.run(context.block(screen, history))
    assert text.startswith("<context>") and text.endswith("</context>")
    assert text.index("Panel 1 · Web search") < text.index("Panel 2 · Download")   # in number order
    assert "Panel 1 · Web search · nebula.search [FOCUSED]" in text
    assert "(still running: 3 of 10 bytes)" in text
    assert "The user has panel 1 focused" in text
    assert '- "frieren ost on youtube" → nebula.youtube:' in text and "Frieren OST" in text
    assert text.count('"#":3') == 1


def test_focus_on_a_panel_that_isnt_there_is_ignored(monkeypatch):
    monkeypatch.setattr(context, "results", fake_results({"a": done("t.x", {"v": 1})}))
    text = asyncio.run(context.block(Screen(panels=[PanelRef(n=1, task_id="a", tool="t.x")], focus=4), []))
    assert "FOCUSED" not in text and "focused" not in text


def test_nothing_to_add_means_no_block():
    assert asyncio.run(context.block(None, [{"role": "user", "text": "hi"}])) == ""


def test_stored_turns_become_alternating_messages_with_their_panel():
    history = [{"role": "assistant", "text": "orphan"},                    # trimmed list may start mid-turn
               {"role": "user", "text": "summarize it", "about": "2 · Page"},
               {"role": "user", "text": "and lock the door"},              # two sent at once
               {"role": "assistant", "text": "Summary…"},
               {"role": "assistant", "text": "Locked."}]
    msgs = context.messages(history)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[0].text == "[about panel 2 · Page] summarize it\n\nand lock the door"
    assert msgs[1].text == "Summary…\n\nLocked."


def test_session_ids_from_the_display_are_checked():
    assert context.valid_session("0f3a9c1e5b7d4e8a9c217d4e5f6a8b90")
    assert not context.valid_session("short") and not context.valid_session("../../etc") and not context.valid_session(None)
