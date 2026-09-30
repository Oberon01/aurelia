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


@pytest.mark.parametrize("said,hit", [("tv pc", True), ("the TV PC", True), ("tv", True), ("tv-pc", True),
                                      ("living room tv", False), ("the living room tv", False), ("bedroom", False)])
def test_a_computer_called_tv_pc_doesnt_claim_the_living_room_tv(said, hit):
    assert constellation._match(said, "TV PC", "tv-pc") is hit


def test_which_tv_input_a_computer_comes_through():
    class Policy:
        rules = {"screens": [{"computer": "TV PC", "tv": "Living room TV", "input": "HDMI 2"},
                             {"computer": "Desk", "tv": "Office TV", "input": ""}, "junk"]}

    class App:
        class state:
            class governance:
                functions = {"constellation": Policy}
    assert constellation.tv_link(App, {"name": "TV PC", "hostname": "tv-pc"}) == {"tv": "Living room TV", "input": "HDMI 2"}
    assert constellation.tv_link(App, {"name": "Desk", "hostname": "desk"}) is None          # no input set: nothing to do
    assert constellation.tv_link(App, {"name": "Laptop", "hostname": "lap"}) is None


# ---- node health ----------------------------------------------------------------------------------

from app import nodes  # noqa: E402


def test_what_a_check_in_says_is_wrong():
    r = nodes.HEALTH_DEFAULTS
    v = {"cpu": 95, "memory": 93, "swap": 40, "disks": [{"mount": "/", "used": 98, "free_gb": 2}, {"mount": "/data", "used": 50}],
         "temp": 88, "battery": {"percent": 6, "charging": False}}
    found = {i["key"]: i for i in nodes.issues(v, [95, 96, 97], r)}
    assert set(found) == {"disk:/", "memory", "temp", "cpu", "battery"}
    assert found["disk:/"]["level"] == "urgent" and found["disk:/"]["text"] == "Disk / is 98% full (2 GB free)"
    assert found["temp"]["level"] == "warn" and "swapping" in found["memory"]["text"]
    assert "cpu" not in {i["key"] for i in nodes.issues(v, [95, 20, 97], r)}               # busy once isn't a problem
    assert nodes.issues({"cpu": 5, "memory": 30, "disks": [{"mount": "/", "used": 40}]}, [5], r) == []


def test_what_a_node_reports_is_bounded():
    t = nodes.clean_tree({"a": "x" * 500, "b": list(range(100)), "c": {"d": {"e": {"f": {"g": {"h": 1}}}}}, "f": object()})
    assert len(t["a"]) == 120 and len(t["b"]) == 20 and t["c"]["d"]["e"]["f"]["g"] is None and t["f"] is None


def test_why_its_slow_in_plain_words():
    d = {"cpu_percent": 93, "memory": {"used_percent": 95, "available_gb": 0.4}, "swap": {"used_percent": 40},
         "disks": [{"mount": "/", "used_percent": 60, "free_gb": 90}], "specs": {"cores": 4},
         "groups": [{"name": "chrome", "count": 38, "cpu_percent": 70.2, "memory_mb": 3100}]}
    found = constellation.diagnose(d, nodes.HEALTH_DEFAULTS)
    assert found[0] == {"short": "CPU busy (Chrome)", "text": "The CPU is 93% busy; most of it is Chrome (38 processes, 70%)"}
    assert "swapping" in found[1]["text"] and "Chrome (3.1 GB)" in found[1]["text"]
    ok = constellation.diagnose({"cpu_percent": 5, "memory": {"used_percent": 30}, "disks": [{"used_percent": 40}]},
                                nodes.HEALTH_DEFAULTS)
    assert ok == [{"short": "running fine", "text": "Nothing looks wrong: CPU 5%, memory 30%, fullest disk 40%"}]
