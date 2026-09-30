"""Constellation step 4 on its own: amounts of time, the confirmation it hands back, and that
the assistant only holds a confirmation for the same function's own tool."""
import asyncio
import json
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://unused/none")     # the assistant module reads settings on import
os.environ.setdefault("REDIS_URL", "redis://unused:6379/0")
from app import assistant, constellation  # noqa: E402


@pytest.mark.parametrize("said,secs", [("90", 90), ("2 minutes", 120), ("1:30", 90), ("1 min 30 s", 90),
                                       ("1.5 minutes", 90), ("1:02:03", 3723), ("30 seconds", 30), ("", None), ("ten", None)])
def test_amounts_of_time(said, secs):
    assert constellation.seconds_in(said) == secs


def test_video_position_reads_like_a_player():
    v = constellation._video({"paused": False, "time": 83, "duration": 3725, "muted": False, "volume": 40, "speed": 1})
    assert v["position"] == "1:23 / 1:02:05"
    assert constellation._video({"time": 5, "live": True, "duration": None})["position"] == "0:05"


def test_a_risky_click_becomes_a_submit_he_confirms():
    node = {"id": 1, "name": "TV PC"}
    out = constellation._confirm(node, constellation.ConfirmFirst("Place order", "click"), {"n": 6, "text": ""})
    assert out["confirm"]["tool"] == "constellation.submit"
    assert out["confirm"]["args"] == {"target": "TV PC", "expect": "Place order", "n": 6}
    assert out["confirm"]["reason"] == '"Place order" on TV PC looks like it buys, pays, sends, posts or deletes something. Press it?'
    out = constellation._confirm(node, constellation.ConfirmFirst("Message", "enter"), {})
    assert out["confirm"]["args"] == {"target": "TV PC", "enter": True, "expect": "Message"}
    assert "would send it" in out["confirm"]["reason"]


class FakeRedis:
    def __init__(self):
        self.kv = {}

    async def set(self, k, v, ex=None):
        self.kv[k] = v


class FakeApp:
    class state:
        redis = FakeRedis()


def test_the_assistant_holds_a_tools_own_follow_up():
    out = {"needs_confirmation": []}
    res = {"status": "completed", "result": json.dumps({"confirm": {"tool": "constellation.submit", "args": {"n": 6},
                                                                    "reason": "Press it?", "reply": "Pressed."}})}
    why = asyncio.run(assistant._follow_up(FakeApp, "owner", "constellation.click", res, out))
    assert why == "Press it?" and out["needs_confirmation"][0]["tool"] == "constellation.submit"
    held = json.loads(FakeApp.state.redis.kv[assistant.PENDING_PREFIX + out["needs_confirmation"][0]["action_id"]])
    assert held == {"tool": "constellation.submit", "args": {"n": 6}, "user": "owner", "reply": "Pressed."}


@pytest.mark.parametrize("result", [
    {"status": "completed", "result": {"confirm": {"tool": "orbit.unlock", "args": {}}}},     # another function's tool
    {"status": "failed", "result": {"confirm": {"tool": "constellation.submit"}}},
    {"status": "completed", "result": {"headline": "fine"}},
    None])
def test_nothing_else_is_held(result):
    out = {"needs_confirmation": []}
    assert asyncio.run(assistant._follow_up(FakeApp, "owner", "constellation.click", result, out)) is None
    assert out["needs_confirmation"] == []
