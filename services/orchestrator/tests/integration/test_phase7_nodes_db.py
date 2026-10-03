"""Phase 7 step 2: a real node agent (a separate process) pairs as a node, connects,
and does what Aurelia asks, against real Postgres and Redis. A stand-in xdg-open and
gtk-launch record what the agent was asked to start."""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

from .conftest import DB, REDIS

pytestmark = pytest.mark.skipif(not (DB and REDIS), reason="needs TEST_DATABASE_URL and TEST_REDIS_URL")
AGENT = Path(__file__).resolve().parents[2] / "app" / "node_agent" / "aurelia_node.py"


def run_task(orch, tool, args, wait=20):
    r = httpx.post(f"{orch}/tasks", json={"tool": tool, "args": args})
    assert r.status_code == 202, r.text
    tid = r.json()["task_id"]
    deadline = time.time() + wait
    while time.time() < deadline:
        t = httpx.get(f"{orch}/tasks/{tid}").json()
        if t["status"] in ("completed", "failed"):
            res = t["result"] if isinstance(t["result"], (dict, type(None))) else json.loads(t["result"])
            return t["status"], res, t.get("error")
        time.sleep(0.2)
    raise AssertionError("task never finished")


@pytest.fixture
def node(stack, tmp_path, request):
    orch = stack["orch"]
    import asyncio, asyncpg
    async def clean():
        c = await asyncpg.connect(DB)
        await c.execute("DELETE FROM devices; DELETE FROM mailbox_items WHERE kind='device'")
        await c.close()
    asyncio.run(clean())
    httpx.post(f"{orch}/pair/request", json={"kind": "node", "name": "decoy"}).raise_for_status()   # not approved: never used
    p = httpx.post(f"{orch}/pair/request", json={"kind": "node", "name": "Living room TV",
                                                  "info": {"os": "Linux", "hostname": "tv-pc"}}).json()
    [item] = [i for i in httpx.get(f"{orch}/mailbox").json()["items"] if i["proposal"].get("device_id") == p["device_id"]]
    httpx.post(f"{orch}/mailbox/{item['id']}/decide", json={"action": "approve"}).raise_for_status()
    got = httpx.post(f"{orch}/pair/status", json={"device_id": p["device_id"], "claim": p["claim"]}).json()

    home, bin_, record = tmp_path / "home", tmp_path / "bin", tmp_path / "started.txt"
    (home / ".config" / "aurelia-node").mkdir(parents=True)
    (home / ".config" / "aurelia-node" / "config.json").write_text(json.dumps({"server": orch, "device_id": got["device_id"], "name": got["name"]}))
    (home / ".config" / "aurelia-node" / "key").write_text(got["key"])
    apps = home / ".local" / "share" / "applications"
    apps.mkdir(parents=True)
    (apps / "org.videolan.vlc.desktop").write_text("[Desktop Entry]\nType=Application\nName=VLC media player\nExec=vlc\n")
    bin_.mkdir()
    for tool in ("xdg-open", "gtk-launch"):
        (bin_ / tool).write_text(f"#!/bin/sh\necho \"{tool} $1\" >> {record}\n")
        (bin_ / tool).chmod(0o755)
    env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "PATH": f"{bin_}:/usr/bin:/bin",
           "AURELIA_MPV_ARGS": "--vo=null --ao=null"}                # her player, with nothing to show or hear
    marker = request.node.get_closest_marker("node_env")
    env.update(marker.kwargs if marker else {})
    proc = subprocess.Popen([sys.executable, str(AGENT), "run"], env=env, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 10
    while time.time() < deadline:                  # online once it has said hello and is polling
        devs = run_task(orch, "constellation.devices", {})[1]["paired"]
        if any(d["name"] == "Living room TV" and d.get("online") for d in devs):
            break
        time.sleep(0.3)
    yield {"orch": orch, "record": record, "proc": proc, "id": got["device_id"], "key": got["key"]}
    proc.kill()
    subprocess.run(["pkill", "-f", r"^\S*mpv --idle=yes"], capture_output=True)   # her player outlives the agent (by design)


def started(node):
    deadline = time.time() + 5
    while time.time() < deadline and not node["record"].exists():
        time.sleep(0.1)
    time.sleep(0.2)
    return node["record"].read_text().splitlines() if node["record"].exists() else []


def test_a_node_says_what_it_is(node):
    devs = run_task(node["orch"], "constellation.devices", {})[1]["paired"]
    tv = next(d for d in devs if d["name"] == "Living room TV")
    assert tv["online"] and tv["kind"] == "node" and tv["os"]           # its own report replaced what it asked with


def test_aurelia_opens_a_page_and_an_app_on_it(node):
    status, res, _ = run_task(node["orch"], "constellation.open", {"url": "youtube.com", "target": "living room"})
    assert status == "completed" and res["headline"] == "Asked Living room TV's browser to open it"   # its own browser: not seen
    assert res["opened"]["unconfirmed"] is True
    status, res, _ = run_task(node["orch"], "constellation.open", {"app": "vlc", "target": "the tv"})
    assert status == "completed" and res["opened"]["what"] == "VLC media player"
    assert started(node) == ["xdg-open https://youtube.com", "gtk-launch org.videolan.vlc"]
    status, res, _ = run_task(node["orch"], "constellation.apps", {"target": "tv"})
    assert status == "completed" and "VLC media player" in [a["name"] for a in res["apps"]["items"]]


def test_she_refuses_clearly(node):
    for args, msg in [({"url": "file:///etc/passwd"}, "only open web pages"),
                      ({"app": "steam", "target": "tv"}, "no app called steam"),
                      ({"url": "https://x.org", "target": "kitchen"}, "don't know a computer called"),
                      ({}, "Say a web page or an app")]:
        status, _, err = run_task(node["orch"], "constellation.open", args)
        assert status == "failed" and msg in err, (args, err)


def test_only_its_own_key_and_kind_reach_the_node_api(node, stack):
    orch = stack["orch"]
    assert httpx.get(f"{orch}/nodes/commands?wait=0").status_code == 401
    assert httpx.get(f"{orch}/nodes/commands?wait=0", headers={"Authorization": "Bearer aur_nope"}).status_code == 401


def test_a_long_poll_really_waits(node):
    """redis-py 8 reads with a 5 s timeout by default; a node's wait must outlast it."""
    t = time.time()
    r = httpx.get(f"{node['orch']}/nodes/commands?wait=8", headers={"Authorization": f"Bearer {node['key']}"}, timeout=20)
    assert r.status_code == 200 and r.json()["commands"] == [] and time.time() - t >= 7.5


def test_a_revoked_node_stops_and_goes_offline(node):
    httpx.post(f"{node['orch']}/devices/{node['id']}/revoke").raise_for_status()
    node["proc"].wait(timeout=40)
    assert node["proc"].returncode == 3 and "revoked" in node["proc"].stderr.read()
    status, _, err = run_task(node["orch"], "constellation.open", {"url": "https://x.org"})
    assert status == "failed" and "None of your computers" in err


def test_the_agent_and_installer_are_served_without_a_key(stack):
    orch = stack["orch"]
    agent = httpx.get(f"{orch}/node/agent.py")
    assert agent.status_code == 200 and "VERSION = " in agent.text
    inst = httpx.get(f"{orch}/node/install-linux.sh")
    assert inst.status_code == 200 and "systemctl --user" in inst.text
    for path in ("/node/agent.py", "/node/install-linux.sh"):
        assert httpx.get(f"{orch}/auth/check", headers={"X-Forwarded-Uri": path}).status_code == 200


def test_constellation_answers_its_health_check(node):
    """Chrysalis checks every registered function's /health after a deploy."""
    fns = httpx.get(f"{node['orch']}/registry/functions").json()
    fns = fns.get("functions", fns) if isinstance(fns, dict) else fns
    for f in fns:
        if f["name"] in ("constellation", "mnemosyne"):
            r = httpx.get(f"{f['url'].rstrip('/')}/health")
            assert r.status_code == 200 and r.json()["status"] == "ok", f["name"]
    assert httpx.get(f"{node['orch']}/constellation/health").json()["online"] == 1


def test_open_x_on_y_prefers_the_computer_and_opens_sites(node):
    """'open youtube on the tv': YouTube isn't an installed app there, so its site opens in the browser."""
    status, res, err = run_task(node["orch"], "constellation.open", {"what": "youtube", "target": "tv"})
    assert status == "completed", err
    assert res["opened"] == {"node": "Living room TV", "what": "https://www.youtube.com", "kind": "page", "unconfirmed": True}
    status, res, _ = run_task(node["orch"], "constellation.open", {"what": "vlc", "target": "living room"})
    assert status == "completed" and res["opened"]["kind"] == "app"                # a real app wins over a site
    status, res, _ = run_task(node["orch"], "constellation.open", {"what": "twitch.tv", "target": "tv"})
    assert status == "completed" and res["opened"]["what"] == "https://twitch.tv"
    assert started(node)[-3:] == ["xdg-open https://www.youtube.com", "gtk-launch org.videolan.vlc", "xdg-open https://twitch.tv"]


def test_no_computer_by_that_name_falls_back_to_a_roku(node):
    """The test stack has no Orbit, so the Roku fallback is refused and she says which computers exist."""
    status, _, err = run_task(node["orch"], "constellation.open", {"what": "netflix", "target": "bedroom tv"})
    assert status == "failed" and "don't know a computer called" in err and "Living room TV" in err


def test_a_roku_by_that_name_gets_the_app(node, stack):
    """Orbit's tv_app runs as its own task (same governance), and her answer names the TV."""
    orch = stack["orch"]
    httpx.post(f"{orch}/registry/functions", json={"name": "orbit", "description": "test", "url": stack["fn"],
        "tools": [{"name": "tv_app", "description": "open an app on a TV", "input_schema": {"type": "object"}}]}).raise_for_status()
    status, res, err = run_task(orch, "constellation.open", {"what": "netflix", "target": "bedroom tv"})
    assert status == "completed", err
    assert res["opened"]["kind"] == "tv" and res["headline"].startswith("Opened on")
    import asyncio, asyncpg
    async def q():
        c = await asyncpg.connect(DB)
        try:
            return await c.fetchrow("SELECT args, trigger FROM task_runs WHERE tool='orbit.tv_app' ORDER BY started_at DESC LIMIT 1")
        finally:
            await c.close()
    row = asyncio.run(q())
    assert json.loads(row["args"]) == {"name": "bedroom tv", "app": "netflix"} and row["trigger"] == "constellation"


def test_a_computer_on_a_tv_input_brings_the_tv_with_it(node, stack):
    """The TV PC is plugged into a Roku TV: opening something on it also has Orbit turn that TV on
    and switch it to the PC's input (Constellation's `screens`), as its own governed task."""
    import asyncio, asyncpg
    orch = stack["orch"]
    httpx.post(f"{orch}/devices/{node['id']}/rename", json={"name": "Den PC"}).raise_for_status()

    def tv_tasks():
        async def q():
            c = await asyncpg.connect(DB)
            try:
                return await c.fetch("SELECT tool, args, trigger FROM task_runs WHERE tool LIKE 'orbit.%' ORDER BY started_at")
            finally:
                await c.close()
        return [(r["tool"], json.loads(r["args"]), r["trigger"]) for r in asyncio.run(q())]
    before = len(tv_tasks())

    # Orbit isn't there: the page still opens, and she says the TV didn't switch.
    status, res, err = run_task(orch, "constellation.open", {"url": "https://example.com/a", "target": "den pc"})
    assert status == "completed", err
    assert res["opened"]["what"] == "https://example.com/a" and "couldn't switch Den TV to HDMI 2" in res["tv_note"]

    httpx.post(f"{orch}/registry/functions", json={"name": "orbit", "description": "test", "url": stack["fn"], "tools": [
        {"name": "tv_app", "description": "app", "input_schema": {"type": "object"}},
        {"name": "tv_input", "description": "input", "input_schema": {"type": "object"}}]}).raise_for_status()
    status, res, err = run_task(orch, "constellation.open", {"url": "https://example.com/b", "target": "den pc"})
    assert status == "completed", err
    assert res["tv"] == "Den TV on HDMI 2" and "tv_note" not in res
    # A web page "on the den tv" goes to the computer plugged into it (a Roku can't open pages)…
    status, res, err = run_task(orch, "constellation.open", {"what": "example.com/c", "target": "den tv"})
    assert status == "completed" and res["opened"]["node"] == "Den PC" and res["tv"] == "Den TV on HDMI 2", err
    # …an app name there is the Roku's own app.
    status, res, err = run_task(orch, "constellation.open", {"what": "netflix", "target": "den tv"})
    assert status == "completed" and res["opened"]["kind"] == "tv", err
    assert started(node)[-3:] == ["xdg-open https://example.com/a", "xdg-open https://example.com/b", "xdg-open https://example.com/c"]
    assert tv_tasks()[before:] == [("orbit.tv_input", {"name": "Den TV", "input": "HDMI 2"}, "constellation"),
                                   ("orbit.tv_input", {"name": "Den TV", "input": "HDMI 2"}, "constellation"),
                                   ("orbit.tv_app", {"name": "den tv", "app": "netflix"}, "constellation")]

    # A link without an input set does nothing to any TV.
    httpx.post(f"{orch}/devices/{node['id']}/rename", json={"name": "Attic PC"}).raise_for_status()
    status, res, err = run_task(orch, "constellation.open", {"url": "https://example.com/d", "target": "attic pc"})
    assert status == "completed" and "tv" not in res and "tv_note" not in res, err
    assert len(tv_tasks()) == before + 3


def test_she_reports_how_a_computer_is_doing_and_closes_only_what_he_confirms(node, stack):
    """Node reporting with a real agent: hardware and live health, what's running, and closing a
    program only through his confirmation (the exact process, never the system's)."""
    orch = stack["orch"]
    status, res, err = run_task(orch, "constellation.health", {"target": "living room tv"}, wait=30)
    assert status == "completed", err
    h = res["health"]
    assert h["specs"]["cpu"] and h["specs"]["memory_gb"] > 0 and h["memory"]["used_percent"] >= 0
    assert h["disks"] and isinstance(h["groups"], list) and res["diagnosis"] and res["headline"].startswith("Living room TV: ")

    sleeper = subprocess.Popen(["sleep", "300"])                 # a program of his to close
    try:
        status, res, err = run_task(orch, "constellation.list_processes", {"target": "tv", "name": "sleep"}, wait=30)
        assert status == "completed", err
        mine = [p for p in res["processes"]["items"] if p["pid"] == sleeper.pid]
        assert mine and mine[0]["name"] == "sleep" and "cmdline" not in mine[0]          # names only, never command lines

        status, res, err = run_task(orch, "constellation.close_process", {"target": "tv", "name": "sleep"}, wait=30)
        assert status == "completed", err
        c = res["confirm"]
        assert c["tool"] == "constellation.kill_process" and "Close Sleep on Living room TV" in c["reason"]
        assert {"pid": sleeper.pid, "start_key": mine[0]["start_key"]} in c["args"]["targets"]
        assert sleeper.poll() is None                                                   # nothing closed yet
        r = httpx.post(f"{orch}/tasks", json={"tool": "constellation.kill_process", "args": c["args"]})
        assert r.status_code == 409                                                     # a change: he confirms
        tid = httpx.post(f"{orch}/tasks", json={"tool": "constellation.kill_process", "args": c["args"],
                                                "confirmed": True}).json()["task_id"]
        deadline = time.time() + 20
        while time.time() < deadline and httpx.get(f"{orch}/tasks/{tid}").json()["status"] not in ("completed", "failed"):
            time.sleep(0.2)
        t = httpx.get(f"{orch}/tasks/{tid}").json()
        done = t["result"] if isinstance(t["result"], dict) else json.loads(t["result"])
        assert done["headline"] == "Closed Sleep" and sleeper.wait(5) is not None
    finally:
        sleeper.kill()

    status, _, err = run_task(orch, "constellation.close_process", {"target": "tv", "name": "no-such-program"}, wait=30)
    assert status == "failed" and "Nothing called no-such-program is running" in err
    stale = httpx.post(f"{orch}/tasks", json={"tool": "constellation.kill_process", "confirmed": True, "args": {
        "target": "tv", "targets": [{"pid": node["proc"].pid, "start_key": "1"}]}}).json()["task_id"]
    deadline = time.time() + 20
    while time.time() < deadline and httpx.get(f"{orch}/tasks/{stale}").json()["status"] not in ("completed", "failed"):
        time.sleep(0.2)
    t = httpx.get(f"{orch}/tasks/{stale}").json()
    out = t["result"] if isinstance(t["result"], dict) else json.loads(t["result"])
    assert out["headline"].startswith("Didn't close") and node["proc"].poll() is None   # its own agent survives


def test_a_check_in_carries_health_and_urgent_problems_reach_the_mailbox(node, stack):
    orch = stack["orch"]
    import asyncio, asyncpg
    async def q(sql, *a):
        c = await asyncpg.connect(DB)
        try:
            return await c.fetch(sql, *a)
        finally:
            await c.close()
    asyncio.run(q("DELETE FROM mailbox_items WHERE kind='node_alert'"))
    devs = run_task(orch, "constellation.devices", {"computers": True})[1]["paired"]
    tv = [d for d in devs if d["name"] == "Living room TV"][0]
    assert "cpu" in tv["vitals"] and isinstance(tv["issues"], list)                     # its first check-in's reading

    vitals = {"cpu": 12, "memory": 40, "swap": 0, "disks": [{"mount": "/", "used": 98, "free_gb": 2}], "temp": 60}
    hello = lambda: httpx.post(f"{orch}/nodes/hello", headers={"Authorization": f"Bearer {node['key']}"},
                               json={"info": {"os": "Linux"}, "specs": {"cpu": "Test CPU", "cores": 4}, "vitals": vitals})
    hello().raise_for_status()
    hello().raise_for_status()                                                          # once a day per problem
    cards = asyncio.run(q("SELECT id, title, urgent FROM mailbox_items WHERE kind='node_alert'"))
    assert len(cards) == 1 and cards[0]["title"] == "Living room TV: disk / is 98% full (2 GB free)" and cards[0]["urgent"]
    devs = run_task(orch, "constellation.devices", {"computers": True})[1]["paired"]
    tv = [d for d in devs if d["name"] == "Living room TV"][0]
    assert tv["issues"][0]["level"] == "urgent" and tv["vitals"]["disks"][0]["used"] == 98
    out = httpx.post(f"{orch}/mailbox/{cards[0]['id']}/decide", json={"action": "approve"}).json()
    assert out["done"].startswith("Noted.")


def test_whats_wrong_with_a_computer(node, stack):
    """Stage 1 of node fixes, with a real agent: her read-only checks come back worst first, with
    anything the last check-in flagged, and nothing on the computer is changed."""
    orch = node["orch"]
    status, res, err = run_task(orch, "constellation.diagnose", {"target": "living room tv"}, wait=60)
    assert status == "completed", err
    ids = [c["id"] for c in res["checks"]]
    assert {"services", "her", "errors", "kernel", "network", "tailscale", "time", "storage", "memory", "updates"} <= set(ids)
    order = {"bad": 0, "warn": 1, "info": 2, "ok": 3}
    ranks = [order[c["status"]] for c in res["checks"]]
    assert ranks == sorted(ranks)                                         # worst first
    assert res["node"] == "Living room TV" and res["headline"].startswith("Living room TV")
    status, res, err = run_task(orch, "constellation.diagnose", {"target": "living room tv", "service": "cron"}, wait=60)
    assert status == "completed" and any(c["id"].startswith("service:cron") for c in res["checks"])
    status, res, err = run_task(orch, "constellation.diagnose", {"target": "living room tv", "service": "a;b"}, wait=60)
    assert status == "failed" and "service name" in err


def test_power_restart_and_wake(node, stack):
    """Power with a real agent: idle and lock answer, a restart is held for his confirmation and the
    computer refuses to try one it can't confirm it may do, and waking a computer that's off sends the
    signal from another computer on the network."""
    import json as _json
    import asyncio, asyncpg
    orch = node["orch"]
    status, res, err = run_task(orch, "constellation.power", {"target": "living room tv", "action": "idle"}, wait=30)
    assert status == "completed" and res["headline"].startswith("Living room TV")
    r = httpx.post(f"{orch}/tasks", json={"tool": "constellation.restart", "args": {"target": "living room tv", "action": "restart"}})
    assert r.status_code == 409 and r.json()["detail"]["needs_confirmation"]                 # always his call
    tid = httpx.post(f"{orch}/tasks", json={"tool": "constellation.restart", "args": {"target": "living room tv"},
                                            "confirmed": True}).json()["task_id"]
    for _ in range(100):
        row = httpx.get(f"{orch}/tasks/{tid}").json()
        if row["status"] != "running":
            break
        time.sleep(0.2)
    assert row["status"] == "failed" and "couldn't check whether I'm allowed" in row["error"]    # no systemd here: no blind try

    # Wake a computer that's off: another one on the network sends the signal.
    async def add_sleeper():
        conn = await asyncpg.connect(DB)
        try:
            return await conn.fetchval(
                "INSERT INTO devices (kind, name, status, key_hash, approved_at, info) VALUES ('node', 'Desk PC', 'approved', "
                "'x' || md5(random()::text), now(), $1::jsonb) RETURNING id",
                _json.dumps({"hostname": "desk", "specs": {"mac": "aa:bb:cc:dd:ee:ff", "broadcast": "127.255.255.255"}}))
        finally:
            await conn.close()
    asyncio.run(add_sleeper())
    status, res, err = run_task(orch, "constellation.wake", {"target": "desk pc"}, wait=30)
    assert status == "completed", err
    assert res["headline"] == "Waking Desk PC" and "from Living room TV" in res["power"]["note"]
    status, res, err = run_task(orch, "constellation.wake", {"target": "living room tv"}, wait=30)
    assert res["headline"] == "Living room TV is already on"


FAKE_WPCTL = r'''#!PY
import json, sys, pathlib
st = pathlib.Path(__file__).with_name("sound.json")
s = json.loads(st.read_text()) if st.exists() else {"vol": 0.5, "muted": False, "default": "49"}
a = sys.argv[1:]
if a[0] == "status":
    mark = lambda i: " *  " if s["default"] == i else "    "
    print("Audio\n ├─ Sinks:\n │ " + mark("49") + " 49. Built-in Audio Analog Stereo  [vol: 0.50]\n │ " + mark("55")
          + " 55. GA104 HDMI Output  [vol: 0.50]\n │\n └─ Sources:\n")
elif a[0] == "get-volume":
    print(f"Volume: {s['vol']:.2f}" + (" [MUTED]" if s["muted"] else ""))
elif a[0] == "set-volume":
    v = a[-1]
    s["vol"] = float(v) if "%" not in v else min(1.0, s["vol"] + (1 if v.endswith("+") else -1) * int(v[:-2]) / 100)
elif a[0] == "set-mute":
    s["muted"] = {"1": True, "0": False}.get(a[-1], not s["muted"])
elif a[0] == "set-default":
    s["default"] = a[-1]
st.write_text(json.dumps(s))
'''
FAKE_PLAYERCTL = r'''#!PY
import sys, pathlib
st = pathlib.Path(__file__).with_name("track.txt")
t = st.read_text() if st.exists() else "One More Time"
a = sys.argv[1:]
if "metadata" in a:
    print(f"spotify\tPlaying\tDaft Punk\t{t}\tDiscovery\t1:02\t5:20")
elif a[-1] == "next":
    st.write_text("Aerodynamic")
with open(pathlib.Path(__file__).with_name("played.txt"), "a") as f:
    f.write(" ".join(a) + "\n")
'''


def test_sound_and_music(node):
    """Sound and media players with a real agent (stand-in wpctl and playerctl): volume, mute and
    the output; next track and what's playing; pause reaches the player when there's no shell page."""
    orch, bin_ = node["orch"], node["record"].parent / "bin"
    for name, src in (("wpctl", FAKE_WPCTL), ("playerctl", FAKE_PLAYERCTL)):
        (bin_ / name).write_text(src.replace("#!PY", f"#!{sys.executable}"))
        (bin_ / name).chmod(0o755)
    status, res, err = run_task(orch, "constellation.sound", {"target": "living room tv", "action": "set", "level": 30}, wait=30)
    assert status == "completed", err
    assert res["headline"] == "Living room TV: volume 30%" and res["sound"]["output"] == "Built-in Audio Analog Stereo"
    status, res, err = run_task(orch, "constellation.sound", {"action": "up"}, wait=30)
    assert res["sound"]["volume"] == 40
    status, res, err = run_task(orch, "constellation.sound", {"action": "mute"}, wait=30)
    assert res["sound"]["muted"] and res["headline"] == "Living room TV: muted (volume 40%)"
    status, res, err = run_task(orch, "constellation.sound", {"action": "output", "device": "hdmi"}, wait=30)
    assert status == "completed" and res["headline"] == "Living room TV: sound now plays through GA104 HDMI Output"
    status, res, err = run_task(orch, "constellation.sound", {"action": "output", "device": "bluetooth"}, wait=30)
    assert status == "failed" and "No output like that" in err
    status, res, err = run_task(orch, "constellation.sound", {"action": "set"}, wait=30)
    assert status == "failed" and "to what" in err

    status, res, err = run_task(orch, "constellation.media", {"action": "now_playing"}, wait=30)
    assert status == "completed", err
    assert res["headline"] == "Playing: Daft Punk — One More Time (spotify)" and res["playing"]["node"] == "Living room TV"
    status, res, err = run_task(orch, "constellation.media", {"action": "next"}, wait=30)
    assert res["headline"] == "Next · Daft Punk — Aerodynamic"
    status, res, err = run_task(orch, "constellation.media", {"action": "pause"}, wait=30)   # no shell page here
    assert status == "completed", err
    played = (bin_ / "played.txt").read_text()
    assert "-p spotify next" in played and "-p spotify pause" in played


def test_watching_needs_her_shell(node):
    status, res, err = run_task(node["orch"], "constellation.watch", {"target": "living room tv"}, wait=30)
    assert status == "failed" and "only while it runs my shell" in err


FAKE_PKG = r'''#!PY
import json, sys, pathlib, os
st = pathlib.Path(__file__).with_name("pkgs.json")
s = json.loads(st.read_text()) if st.exists() else {"installed": ["vlc-bin"], "deps": {"libvlc5": ["vlc"]}}
tool, a = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
with open(pathlib.Path(__file__).with_name("pkg-calls.txt"), "a") as f:
    f.write(tool + " " + " ".join(a) + "\n")
avail = {"vlc": "multimedia player", "vlc-bin": "binaries from VLC", "mpv": "video player", "media-player-x": "another one"}
if tool == "apt-cache":
    if pathlib.Path(__file__).with_name("slow").exists():
        import time; time.sleep(12)                                      # longer than she waits before replying
    print("\n".join(f"{n} - {d}" for n, d in avail.items() if a[-1].lower() in n))
elif tool == "dpkg-query":
    if "${Essential} ${Priority}" in a:
        print("yes required" if a[-1] == "systemd" else "no optional", end="")
    else:
        for n in a[3:]:
            print(n, "installed" if n in s["installed"] else "not-installed")
elif tool == "apt-get":
    print("\n".join(f"Remv {x} [1]" for x in [a[-1], *s["deps"].get(a[-1], [])]))
elif tool in ("pkcon", "pkgcli") and "install" in a:
    s["installed"].append(a[-1]); print("Finished")
elif tool in ("pkcon", "pkgcli") and "remove" in a:
    s["installed"].remove(a[-1]); print("Finished")
elif tool == "snap":
    print("Name Version Publisher Notes Summary")
st.write_text(json.dumps(s))
'''


def test_installing_and_removing_apps_only_with_his_yes(node):
    orch, bin_ = node["orch"], node["record"].parent / "bin"
    for name in ("apt-cache", "dpkg-query", "apt-get", "pkgcli", "snap"):
        (bin_ / name).write_text(FAKE_PKG.replace("#!PY", f"#!{sys.executable}"))
        (bin_ / name).chmod(0o755)
    state = lambda: json.loads((bin_ / "pkgs.json").read_text())["installed"]

    status, res, err = run_task(orch, "constellation.install", {"target": "living room tv", "app": "VLC"}, wait=60)
    assert status == "completed", err
    c = res["confirm"]
    assert c["tool"] == "constellation.install_package" and c["args"] == {"target": "Living room TV", "name": "vlc", "source": "apt"}
    assert "Install vlc (multimedia player) on Living room TV, from Ubuntu's packages?" == c["reason"]
    assert "vlc" not in state()                                                       # nothing installed yet
    r = httpx.post(f"{orch}/tasks", json={"tool": "constellation.install_package", "args": c["args"]})
    assert r.status_code == 409 and r.json()["detail"]["needs_confirmation"]            # always his call
    tid = httpx.post(f"{orch}/tasks", json={"tool": "constellation.install_package", "args": c["args"],
                                            "confirmed": True}).json()["task_id"]
    for _ in range(150):
        row = httpx.get(f"{orch}/tasks/{tid}").json()
        if row["status"] != "running":
            break
        time.sleep(0.2)
    assert row["status"] == "completed", row.get("error")
    assert "vlc" in state()
    assert run_task(orch, "constellation.install", {"app": "vlc"}, wait=60)[1]["headline"] == "vlc is already installed on Living room TV"

    status, res, err = run_task(orch, "constellation.install", {"app": "media"}, wait=60)     # several: he picks
    assert status == "completed" and res["headline"] == "Which media?" and "confirm" not in res
    assert {i["name"] for i in res["packages"]["items"]} == {"media-player-x"}

    status, res, err = run_task(orch, "constellation.uninstall", {"app": "vlc"}, wait=60)
    c = res["confirm"]
    assert c["tool"] == "constellation.remove_package" and c["args"]["removes"] == ["vlc"]
    tid = httpx.post(f"{orch}/tasks", json={"tool": "constellation.remove_package", "args": c["args"],
                                            "confirmed": True}).json()["task_id"]
    for _ in range(150):
        row = httpx.get(f"{orch}/tasks/{tid}").json()
        if row["status"] != "running":
            break
        time.sleep(0.2)
    assert row["status"] == "completed", row.get("error") and "vlc" not in state()
    status, _, err = run_task(orch, "constellation.uninstall", {"app": "x", "package": "systemd"}, wait=60)
    assert status == "failed" and "part of the system" in err
    calls = (bin_ / "pkg-calls.txt").read_text()
    assert "pkgcli -y install --no-autoremove vlc" in calls and calls.count("pkgcli") == 2   # only what he confirmed


def test_a_new_agent_is_announced_with_every_poll(node):
    got = httpx.get(f"{node['orch']}/nodes/commands?wait=0", headers={"Authorization": f"Bearer {node['key']}"}).json()
    assert got["commands"] == [] and got["agent"]["version"] and len(got["agent"]["sha256"]) == 64


def test_a_slow_removal_still_brings_its_card(node):
    """The look-up took longer than she waits before replying: the card still comes, to the display
    that asked, and her reply doesn't claim it's done."""
    import asyncio, uuid, asyncpg
    from app.main import app
    from app.llm.base import Completion, ToolCall, Usage
    from .test_phase7_own_panels_db import listen
    orch, bin_ = node["orch"], node["record"].parent / "bin"
    for name in ("apt-cache", "dpkg-query", "apt-get", "pkgcli", "snap"):
        (bin_ / name).write_text(FAKE_PKG.replace("#!PY", f"#!{sys.executable}"))
        (bin_ / name).chmod(0o755)
    (bin_ / "pkgs.json").write_text(json.dumps({"installed": ["vlc-bin", "vlc"], "deps": {}}))
    (bin_ / "slow").write_text("")

    async def browser():
        c = await asyncpg.connect(DB)
        try:
            return await c.fetchval("INSERT INTO devices (kind, name, status, key_hash, approved_at) VALUES "
                                    "('browser', 'Desk', 'approved', $1, now()) RETURNING id", uuid.uuid4().hex)
        finally:
            await c.close()
    desk = asyncio.run(browser())

    class Model:
        def __init__(self):
            self.steps = [[("constellation__uninstall", {"app": "vlc"})], []]
            self.seen = []

        async def complete(self, route, system, messages, tools):
            self.seen.append(messages)
            calls = self.steps.pop(0) if self.steps else []
            return Completion(text="" if calls else "I'm checking what removing VLC would take with it.", usage=Usage(10, 5),
                              model="fake", provider="fake",
                              tool_calls=[ToolCall(id=uuid.uuid4().hex[:8], name=n, args=a) for n, a in calls])
    model, original = Model(), app.state.llm
    got = []

    async def scenario():
        ws = asyncio.create_task(listen(orch.replace("http", "ws") + "/ws", desk, 30, got))
        await asyncio.sleep(0.8)
        async with httpx.AsyncClient(timeout=60) as c:
            out = (await c.post(f"{orch}/ask", json={"text": "remove vlc", "session_id": uuid.uuid4().hex},
                                headers={"X-Aurelia-Device": str(desk)})).json()
        await ws
        return out
    app.state.llm = model
    try:
        out = asyncio.run(scenario())
    finally:
        app.state.llm = original
    assert out["needs_confirmation"] == []                                   # not there in time for her reply
    assert "don't say it happened" in " ".join(m.text or "" for m in model.seen[-1])   # and she was told it isn't done
    cards = [e for e in got if e.get("type") == "confirm.ask"]
    assert len(cards) == 1, got
    action = cards[0]["data"]["action"]
    assert action["tool"] == "constellation.remove_package" and action["args"]["name"] == "vlc"
    done = httpx.post(f"{orch}/ask/confirm", json={"action_id": action["action_id"], "approve": True},
                      headers={"X-Aurelia-Device": str(desk)}, timeout=60)
    assert done.status_code == 200, done.text
    for _ in range(100):
        if "vlc" not in json.loads((bin_ / "pkgs.json").read_text())["installed"]:
            break
        time.sleep(0.2)
    assert "vlc" not in json.loads((bin_ / "pkgs.json").read_text())["installed"]


FAKE_CLIP = r'''#!PY
import sys, pathlib
st, types = pathlib.Path(__file__).with_name("clip.txt"), pathlib.Path(__file__).with_name("clip-types.txt")
if pathlib.Path(sys.argv[0]).name == "wl-copy":
    st.write_text(sys.stdin.read())
elif "--list-types" in sys.argv:
    print(types.read_text() if types.exists() else "text/plain;charset=utf-8")
else:
    sys.stdout.write(st.read_text() if st.exists() else "")
'''


@pytest.mark.node_env(WAYLAND_DISPLAY="wayland-0", XDG_SESSION_TYPE="wayland")
def test_the_shared_clipboard(node):
    """Text moves between his devices through Aurelia's clipboard: from a computer's clipboard, onto
    another's, and to a display as a Copy card. Never a password or a key; screens can't use it."""
    import asyncio, uuid, asyncpg
    orch, bin_ = node["orch"], node["record"].parent / "bin"
    for name in ("wl-copy", "wl-paste"):
        (bin_ / name).write_text(FAKE_CLIP.replace("#!PY", f"#!{sys.executable}"))
        (bin_ / name).chmod(0o755)
    httpx.delete(f"{orch}/constellation/clipboard")

    async def devices():
        c = await asyncpg.connect(DB)
        try:
            out = []
            for kind, name in (("browser", "Phone"), ("screen", "TV screen")):
                out.append(await c.fetchval("INSERT INTO devices (kind, name, status, key_hash, approved_at) VALUES "
                                            "($1, $2, 'approved', $3, now()) RETURNING id", kind, name, uuid.uuid4().hex))
            return out
        finally:
            await c.close()
    phone, screen = asyncio.run(devices())

    (bin_ / "clip.txt").write_text("https://example.com/recipe")
    status, res, err = run_task(node["orch"], "constellation.copy", {"from": "living room tv"}, wait=30)
    assert status == "completed", err
    assert res["headline"] == "Copied “https://example.com/recipe” from Living room TV"
    assert res["clipboard"]["items"][0]["source"] == "Living room TV"

    r = httpx.post(f"{orch}/constellation/clipboard", json={"text": "grocery list: eggs, rice"},
                   headers={"X-Aurelia-Device": str(phone)})
    assert r.status_code == 200 and r.json()["items"][0]["source"] == "Phone"
    assert httpx.post(f"{orch}/constellation/clipboard", json={"text": "sk-abcdefghijklmnop1234"},
                      headers={"X-Aurelia-Device": str(phone)}).status_code == 400            # never a key
    assert httpx.get(f"{orch}/constellation/clipboard", headers={"X-Aurelia-Device": str(screen)}).status_code == 403

    status, res, err = run_task(orch, "constellation.paste", {"to": "living room tv"}, wait=30)
    assert status == "completed" and res["headline"] == "On Living room TV's clipboard", err
    assert (bin_ / "clip.txt").read_text() == "grocery list: eggs, rice"
    status, res, err = run_task(orch, "constellation.paste", {"to": "phone", "n": 2}, wait=30)
    assert status == "completed", err
    assert "Phone" in res["headline"]                                          # a Copy card for the phone

    (bin_ / "clip-types.txt").write_text("text/plain\nx-kde-passwordManagerHint")
    status, _, err = run_task(orch, "constellation.copy", {"from": "living room tv"}, wait=30)
    assert status == "failed" and "password manager" in err
    status, res, _ = run_task(orch, "constellation.clipboard", {}, wait=30)
    assert [i["text"] for i in res["clipboard"]["items"]] == ["grocery list: eggs, rice", "https://example.com/recipe"]


def test_gaming_and_exam_modes(node):
    """Gaming mode: she keeps out of the way. Exam mode: the agent answers, then stops completely and
    stays stopped (even if started again) until its timer, which a stand-in systemd-run records."""
    orch, bin_, proc = node["orch"], node["record"].parent / "bin", node["proc"]
    (bin_ / "systemd-run").write_text(f"#!/bin/sh\necho \"$@\" > {bin_}/timer.txt\n")
    (bin_ / "systemd-run").chmod(0o755)

    res = run_task(orch, "constellation.mode", {"mode": "gaming"}, wait=30)[1]
    assert res["headline"] == "Gaming mode on Living room TV: I'll keep out of the way"
    status, _, err = run_task(orch, "constellation.open", {"url": "https://example.com"}, wait=30)
    assert status == "failed" and "gaming mode" in err
    assert run_task(orch, "constellation.mode", {"mode": "normal"}, wait=30)[1]["headline"] == "Living room TV is back to normal"

    status, res, err = run_task(orch, "constellation.mode", {"mode": "exam", "hours": 2}, wait=30)
    assert status == "completed", err
    assert res["headline"].startswith("Exam mode on Living room TV for 1 h 59 min") or res["headline"].endswith("for 2 h")
    assert "--on-active=7200" in (bin_ / "timer.txt").read_text()               # it comes back by itself
    assert proc.wait(timeout=20) == 4                                            # and it really stopped
    status, _, err = run_task(orch, "constellation.health", {"target": "living room tv"}, wait=30)
    assert status == "failed" and "exam mode for another 1 h 5" in err
    cfg = Path(node["record"]).parent / "home" / ".config" / "aurelia-node"
    again = subprocess.run([sys.executable, str(AGENT), "run"], env={"HOME": str(cfg.parents[1]), "XDG_CONFIG_HOME": str(cfg.parent),
                                                                     "PATH": f"{bin_}:/usr/bin:/bin"}, timeout=20)
    assert again.returncode == 4                                                  # started during the exam: stays stopped


def test_yt_dlp_is_only_for_paired_computers_and_matches_its_checksum(node):
    import hashlib
    orch, h = node["orch"], {"Authorization": f"Bearer {node['key']}"}
    assert httpx.get(f"{orch}/nodes/ytdlp").status_code == 401
    meta = httpx.get(f"{orch}/nodes/ytdlp", headers=h, timeout=60).json()
    z = httpx.get(f"{orch}/nodes/ytdlp.zip", headers=h, timeout=60).content
    assert hashlib.sha256(z).hexdigest() == meta["sha256"] and meta["bytes"] == len(z) and "yt-dlp" in meta["version"]
