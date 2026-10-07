"""The queue (Phase 12.5) with real processes: a real node agent and real mpv (no picture or sound) playing from a
stand-in Jellyfin. What he queues plays when the video before it ends, judged only by the player's own reports;
near the end he hears what's next; with nothing queued, autoplay plays the top suggestion; and once he says
"stop", nothing follows. Skipped without mpv or ffmpeg."""
import json
import shutil
import threading
import time

import httpx
import pytest

from .conftest import DB, REDIS
from .test_phase7_nodes_db import node, run_task  # noqa: F401
from .test_phase7_quasar_db import jellyfin, ok  # noqa: F401

pytestmark = [pytest.mark.skipif(not (DB and REDIS), reason="needs TEST_DATABASE_URL and TEST_REDIS_URL"),
              pytest.mark.skipif(not (shutil.which("mpv") and shutil.which("ffmpeg")), reason="needs mpv and ffmpeg")]


@pytest.fixture
def heard_next():
    """Every what's-next panel published while the test runs."""
    import redis as redis_sync
    r = redis_sync.Redis.from_url(REDIS)
    for k in r.scan_iter("aurelia:queue:*"):
        r.delete(k)
    r.delete("aurelia:autoplay:off")
    ps = r.pubsub()
    ps.subscribe("aurelia:events")
    got, stop = [], threading.Event()

    def listen():
        while not stop.is_set():
            m = ps.get_message(timeout=0.5)
            if m and m["type"] == "message":
                ev = json.loads(m["data"])
                if ev.get("tool") == "quasar.next" and ev.get("type") == "panel.result":
                    got.append(ev["data"])
    threading.Thread(target=listen, daemon=True).start()
    yield got
    stop.set()
    ps.close()
    for k in [*r.scan_iter("aurelia:queue:*"), *r.scan_iter("aurelia:media:ticket:*")]:   # nothing left for other tests
        r.delete(k)


def playing(orch, node, title_starts, wait=60):
    deadline = time.time() + wait
    seen = None
    while time.time() < deadline:
        now = httpx.get(f"{orch}/quasar/now").json()["playing"]
        mine = [p["now"] for p in now if p["key"] == f"node-{node['id']}"]
        seen = mine[0]["title"] if mine else None
        if seen and seen.startswith(title_starts):
            return mine[0]
        time.sleep(0.5)
    raise AssertionError(f"never played {title_starts!r} (last seen: {seen!r})")


def gone(orch, wait=30):
    """Stopped: the player's last report is in (so no other test sees it playing)."""
    deadline = time.time() + wait
    while time.time() < deadline and httpx.get(f"{orch}/quasar/now").json()["playing"]:
        time.sleep(0.3)
    assert httpx.get(f"{orch}/quasar/now").json()["playing"] == []


def test_the_queue_plays_in_order_then_autoplay_and_stop_means_stop(node, jellyfin, heard_next):
    orch = node["orch"]
    # Nothing playing: what he adds starts now, the rest waits its turn.
    out = ok(orch, "quasar.queue", {"action": "add", "ids": "jf:m1"})
    assert out["headline"].startswith("Playing Big Buck (2008) on Living room TV")
    out = ok(orch, "quasar.queue", {"action": "add", "ids": "jf:m2"})
    assert out["headline"] == "Added Murder on the Orient Express (2017) to your queue on Living room TV"
    assert [i["title"] for i in out["queue"]["items"]] == ["Murder on the Orient Express (2017)"]
    # Near the end (halfway through a short one): he hears what's next.
    deadline = time.time() + 40
    while time.time() < deadline and not any(d["upnext"].get("up_next") for d in heard_next):
        time.sleep(0.5)
    first = next(d for d in heard_next if d["upnext"].get("up_next"))
    assert first["headline"] == "Up next on Living room TV: Murder on the Orient Express (2017)"
    # It ends: the queue goes on by itself.
    playing(orch, node, "Murder on the Orient Express")
    assert any(d["upnext"].get("started") == "Murder on the Orient Express (2017)" for d in heard_next)
    assert ok(orch, "quasar.queue", {"action": "show"})["queue"]["items"] == []
    # Nothing queued: near the end, suggestions from his library; at the end, the top one plays.
    deadline = time.time() + 40
    while time.time() < deadline and not any(d["upnext"].get("catalog") for d in heard_next):
        time.sleep(0.5)
    sug = next(d for d in heard_next if d["upnext"].get("catalog"))
    assert sug["upnext"]["source"] == "library" and sug["upnext"]["autoplay"] == "Frieren · S1E2 · It Didn't Have to Be Magic"
    names = [i["name"] for g in sug["upnext"]["catalog"]["groups"] for i in g["items"]]
    assert "Murder on the Orient Express" not in names                         # never what's playing
    playing(orch, node, "Frieren · S1E2")
    assert any((d["upnext"].get("said") or "").startswith("Autoplay on Living room TV: Frieren") for d in heard_next)
    # He says stop: nothing follows it.
    assert ok(orch, "constellation.media", {"action": "stop"})["headline"].startswith("Stopped Frieren")
    gone(orch)
    time.sleep(3)                                                              # and nothing starts after it
    assert httpx.get(f"{orch}/quasar/now").json()["playing"] == []
    assert ok(orch, "constellation.media", {"action": "now_playing"})["headline"] == "Nothing is playing on Living room TV"


def test_skip_at_the_last_one_moves_to_the_queue(node, jellyfin, heard_next):
    orch = node["orch"]
    ok(orch, "quasar.play", {"what": "big buck"})
    playing(orch, node, "Big Buck")
    ok(orch, "quasar.queue", {"action": "next", "ids": "jf:m2"})
    out = ok(orch, "constellation.media", {"action": "next"})
    assert out["headline"] == "Switched to Murder on the Orient Express (2017) on Living room TV"
    ok(orch, "constellation.media", {"action": "stop"})
    gone(orch)


def test_the_phones_menu_and_its_pick_lists(node, monkeypatch):
    """Skipper: what's running is on the menu, and his computers and lights are there to pick."""
    import asyncio
    import uuid
    import asyncpg
    from pathlib import Path
    from app import skipper
    from app.settings import settings
    orch = node["orch"]
    monkeypatch.setattr(skipper, "reachable", lambda app: {(f["name"], c["label"]) for f in skipper.current()["functions"]
                                                           for c in f["commands"]})   # (this stack has few fast paths)
    (Path(settings.config_path).parent / "menu.yaml").write_text(
        "functions:\n"
        "  - {name: quasar, label: Quasar, commands: [{label: My queue, say: show my queue, tool: quasar.queue}]}\n"
        "  - {name: constellation, label: Constellation, commands: [{label: Device status, say: show my devices, tool: constellation.devices}]}\n"
        "  - {name: orbit, label: Orbit, commands: [{label: Lock up, say: lock up, tool: orbit.lock}]}\n")
    menu = httpx.get(f"{orch}/skipper/menu").json()["functions"]
    by = {f["name"]: f for f in menu}
    assert "quasar" in by and "constellation" in by
    assert any(c["say"] == "show my queue" for c in by["quasar"]["commands"])
    assert "orbit" not in by                                                  # not running in this stack: not offered
    assert httpx.get(f"{orch}/skipper/pick/computers").json()["options"] == ["Living room TV"]   # approved only
    tid = uuid.uuid4()

    async def seed():
        c = await asyncpg.connect(DB)
        try:
            await c.execute("INSERT INTO task_runs (id, tool, status) VALUES ($1, 'orbit.status', 'completed')", tid)
            await c.execute("INSERT INTO panel_snapshots (task_id, tool, args_key, panel) VALUES ($1, 'orbit.status', '', $2::jsonb)",
                            tid, json.dumps({"devices": [{"name": "Porch light", "kind": "light"}, {"name": "Front door", "kind": "lock"},
                                                         {"name": "Kitchen light", "kind": "light"}]}))
        finally:
            await c.close()

    async def unseed():
        c = await asyncpg.connect(DB)
        try:
            await c.execute("DELETE FROM task_runs WHERE id=$1", tid)
        finally:
            await c.close()
    asyncio.run(seed())
    try:
        assert httpx.get(f"{orch}/skipper/pick/lights").json()["options"] == ["Kitchen light", "Porch light"]
        assert httpx.get(f"{orch}/skipper/pick/locks").json()["options"] == ["Front door"]
        assert httpx.get(f"{orch}/skipper/pick/planets").status_code == 404
    finally:
        asyncio.run(unseed())
