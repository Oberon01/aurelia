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
def node(stack, tmp_path):
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
    env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "PATH": f"{bin_}:/usr/bin:/bin"}
    proc = subprocess.Popen([sys.executable, str(AGENT), "run"], env=env, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 10
    while time.time() < deadline:                  # online once it has said hello and is polling
        devs = run_task(orch, "constellation.devices", {})[1]["paired"]
        if any(d["name"] == "Living room TV" and d.get("online") for d in devs):
            break
        time.sleep(0.3)
    yield {"orch": orch, "record": record, "proc": proc, "id": got["device_id"], "key": got["key"]}
    proc.kill()


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
    assert status == "completed" and res["headline"] == "Opened on Living room TV"
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
    assert r.status_code == 200 and r.json() == {"commands": []} and time.time() - t >= 7.5


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
