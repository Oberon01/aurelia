"""Device roles, groups, which panel 'this' means, and quiet hours."""
import os
from datetime import time

os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")    # importing the app needs these set
os.environ.setdefault("REDIS_URL", "redis://localhost")

import pytest

from app import roles
from app.ambient import in_quiet
from app.assistant import _plain, _takes_panel, with_panel_task
from app.context import PanelRef, Screen


@pytest.mark.parametrize("kind,info,want", [
    ("node", {"shell": "on", "screens": [{}]}, "presentation"),
    ("node", {"shell": "off", "screens": [{}]}, "workstation"),
    ("node", {"shell": "off", "screens": []}, "server"),
    ("node", {"shell": "on", "role": "server"}, "server"),             # his --role at install wins over the guess
    ("node", {"role": "toaster"}, "workstation"),                      # nonsense is ignored
    ("browser", {"os": "Android"}, "companion"), ("browser", {"os": "iPhone"}, "companion"),
    ("browser", {"os": "Windows"}, "workstation"), ("terminal", {}, None),
])
def test_guessing(kind, info, want):
    assert roles.effective(kind, None, info) == want


def test_his_choice_wins_and_screens_follow_their_node():
    assert roles.effective("node", "server", {"shell": "on"}) == "server"
    assert roles.effective("browser", "presentation", {"os": "Windows"}) == "presentation"   # a wall tablet
    assert roles.effective("browser", "server", {"os": "Windows"}) == "workstation"          # not a browser role
    assert roles.effective("screen", None, {}, "workstation") == "workstation"
    assert roles.effective("screen", None, {}) == "presentation"


@pytest.mark.parametrize("words,want", [
    ("all presentation devices", "presentation"), ("every presentation display", "presentation"),
    ("all the TVs", "presentation"), ("all my phones", "companion"), ("everywhere", "*"),
    ("all screens", "*"), ("every display", "*"), ("all of them", "*"),
    ("the TV", None), ("my phone", None), ("living room tv", None), ("all", None),
])
def test_groups(words, want):
    assert roles.group(words) == want


def t(n):
    return f"00000000-0000-0000-0000-{n:012d}"          # task ids are UUIDs; live panels (like now playing) aren't


def scr(focus=None, *ns, live=()):
    return Screen(panels=[PanelRef(n=n, task_id=("now-tv" if n in live else t(n)), tool="x.y", queued=(n == 9))
                          for n in ns], focus=focus)


def test_this_panel():
    assert with_panel_task({"to": "tv", "panel": 2}, scr(None, 1, 2, 3))["_panel_task"] == t(2)
    assert with_panel_task({"to": "tv"}, scr(1, 1, 2, 3))["_panel_task"] == t(1)           # the one he clicked
    assert with_panel_task({"to": "tv"}, scr(None, 1, 3, 9))["_panel_task"] == t(3)        # the newest on screen
    assert with_panel_task({"to": "tv"}, scr(None, 1, 2, 3, live=(3,)))["_panel_task"] == t(2)   # "it" skips live ones
    assert with_panel_task({"to": "tv"}, scr(3, 1, 2, 3, live=(3,)))["_panel_task"] == t(2)
    with pytest.raises(ValueError, match="can't be sent"):                                  # named, it's refused plainly
        with_panel_task({"to": "tv", "panel": 3}, scr(None, 1, 3, live=(3,)))
    assert "_panel_task" not in with_panel_task({"to": "tv", "text": "hi"}, scr(None, 1))  # a note, not a panel
    assert "_panel_task" not in with_panel_task({"to": "tv"}, None)
    assert with_panel_task({"to": "tv", "_panel_task": "forged"}, None) == {"to": "tv"}  # never from the model
    with pytest.raises(ValueError, match="no panel 7"):
        with_panel_task({"to": "tv", "panel": 7}, scr(None, 1))


def test_markers_stay_with_us():
    schema = {"type": "object", "properties": {"panel": {"type": "integer", "x-panel": True}}}
    assert _takes_panel(schema) and not _takes_panel({"properties": {"panel": {"type": "integer"}}})
    assert _plain(schema)["properties"]["panel"] == {"type": "integer"}
    assert schema["properties"]["panel"]["x-panel"] is True                 # the original is untouched


def test_quiet_hours():
    assert in_quiet("23:00-07:00", time(23, 30)) and in_quiet("23:00-07:00", time(3, 0))
    assert not in_quiet("23:00-07:00", time(7, 0)) and not in_quiet("23:00-07:00", time(12, 0))
    assert in_quiet("13:00-14:00", time(13, 15)) and not in_quiet("13:00-14:00", time(14, 15))
    assert not in_quiet("", time(3, 0)) and not in_quiet("nonsense", time(3, 0))


def test_long_replies():
    from types import SimpleNamespace
    from app.assistant import first_sentence, long_reply
    cfg = SimpleNamespace(reply_panel_sentences=3, reply_panel_chars=320)
    assert not long_reply("The door is locked.", cfg)
    assert not long_reply("It's locked. The lights are off too.", cfg)
    assert long_reply("One. Two. Three.", cfg)
    assert long_reply("x" * 321, cfg)
    assert long_reply("Steps:\n1) this\n2) that", cfg)
    assert not long_reply("", cfg)
    assert first_sentence("Get the 4070 Ti Super. It has 16 GB. More here.") == "Get the 4070 Ti Super."
    assert first_sentence("word " * 60).endswith("…") and len(first_sentence("word " * 60)) <= 160


def test_which_questions_get_the_stronger_model():
    from app.assistant import is_cloud, needs_reasoning
    from app.config import LLMConfig
    cfg = LLMConfig(routes={"chat": ["claude"], "reason": ["claude_reason", "claude"]})
    for q in ("why did the TV PC hang?", "explain the last release", "how should you improve?",
              "compare these two GPUs", "what do you think about voice next", "what's wrong with the TV PC",
              "what did the last update do", "what would you recommend"):
        assert needs_reasoning(q, cfg), q
    for q in ("lock the door", "open youtube on the tv", "what's the thermostat at", "show my devices", "pause"):
        assert not needs_reasoning(q, cfg), q
    assert not needs_reasoning("why?", LLMConfig(routes={"chat": ["claude"]}))      # no reason route: chat for all
    assert is_cloud("claude") and is_cloud("claude_reason") and not is_cloud("ollama")
