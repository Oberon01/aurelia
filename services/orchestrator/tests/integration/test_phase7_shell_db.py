"""Phase 7 step 3, with real processes: a real agent keeps a real Chromium kiosk on a virtual
display (Xvfb), signed in as the node's screen; Aurelia opens pages in it and brings it home.
And the agent updates itself through its guard, reporting when it can't.

Skipped without Xvfb and a Chromium (the Playwright one is fine)."""
import asyncio
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import asyncpg
import httpx
import pytest

from .conftest import DB, REDIS

CHROME = os.environ.get("AURELIA_TEST_BROWSER") or next(iter(sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))), None)
pytestmark = [pytest.mark.skipif(not (DB and REDIS), reason="needs TEST_DATABASE_URL and TEST_REDIS_URL"),
              pytest.mark.skipif(not (CHROME and shutil.which("Xvfb")), reason="needs Xvfb and Chromium")]
APP = Path(__file__).resolve().parents[2] / "app" / "node_agent"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sql(query, *args):
    async def go():
        c = await asyncpg.connect(DB)
        try:
            return await c.fetch(query, *args)
        finally:
            await c.close()
    return asyncio.run(go())


def run_task(orch, tool, args, wait=20):
    tid = httpx.post(f"{orch}/tasks", json={"tool": tool, "args": args}).json()["task_id"]
    deadline = time.time() + wait
    while time.time() < deadline:
        t = httpx.get(f"{orch}/tasks/{tid}").json()
        if t["status"] in ("completed", "failed"):
            return t["status"], (json.loads(t["result"]) if isinstance(t["result"], str) else t["result"]), t.get("error")
        time.sleep(0.2)
    raise AssertionError("task never finished")


def paired_node(orch, name="Living room TV"):
    sql("DELETE FROM devices")
    sql("DELETE FROM mailbox_items WHERE kind IN ('device','node_update')")
    p = httpx.post(f"{orch}/pair/request", json={"kind": "node", "name": name}).json()
    [item] = [i for i in httpx.get(f"{orch}/mailbox").json()["items"] if i["proposal"].get("device_id") == p["device_id"]]
    httpx.post(f"{orch}/mailbox/{item['id']}/decide", json={"action": "approve"}).raise_for_status()
    return httpx.post(f"{orch}/pair/status", json={"device_id": p["device_id"], "claim": p["claim"]}).json()


def home_for(tmp_path, orch, got, shell=False, agent_bytes=None):
    home = tmp_path / "home"
    cfg = home / ".config" / "aurelia-node"
    cfg.mkdir(parents=True)
    (cfg / "config.json").write_text(json.dumps({"server": orch, "device_id": got["device_id"], "name": got["name"], "shell": shell}))
    (cfg / "key").write_text(got["key"])
    app = home / ".local" / "share" / "aurelia-node"
    app.mkdir(parents=True)
    (app / "aurelia_node.py").write_bytes(agent_bytes or (APP / "aurelia_node.py").read_bytes())
    (app / "aurelia_node_guard.py").write_bytes((APP / "aurelia_node_guard.py").read_bytes())
    return home, app


def env_for(home, **extra):
    return {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "PATH": "/usr/bin:/bin", **extra}


def cdp(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.load(r)


@pytest.fixture
def display():
    """A virtual screen on a display number Xvfb picks itself (free, even right after another test's)."""
    r, w = os.pipe()
    x = subprocess.Popen(["Xvfb", "-displayfd", str(w), "-screen", "0", "1280x720x24"], pass_fds=(w,),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(w)
    with os.fdopen(r) as f:
        n = f.readline().strip()
    assert n.isdigit(), "Xvfb didn't start"
    yield f":{n}"
    x.kill()
    x.wait(10)


def test_the_shell_is_her_screen(stack, tmp_path, display):
    orch = stack["orch"]
    got = paired_node(orch)
    home, app = home_for(tmp_path, orch, got, shell=True)
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(app / "aurelia_node_guard.py"), "run"], stderr=subprocess.PIPE, text=True,
                            env=env_for(home, DISPLAY=display, XDG_CURRENT_DESKTOP="Aurelia", AURELIA_BROWSER=CHROME,
                                        AURELIA_BROWSER_ARGS="--no-sandbox --disable-gpu", AURELIA_DEBUG_PORT=str(port),
                                        AURELIA_POLL_WAIT="2"))
    try:
        deadline, pages = time.time() + 30, []
        while time.time() < deadline:
            try:
                pages = [p for p in cdp(port, "/json/list") if p["type"] == "page"]
                if any(f"/view?screen={got['device_id']}" in p["url"] for p in pages):
                    break
            except OSError:
                pass
            time.sleep(0.5)
        assert any(f"/view?screen={got['device_id']}" in p["url"] for p in pages), pages   # signed in: no pairing page
        [screen] = sql("SELECT status, info->>'node_id' AS node FROM devices WHERE kind='screen'")
        assert screen["status"] == "approved" and screen["node"] == str(got["device_id"])

        page = f"{stack['fn']}/digest?a=1&b=2"                      # somewhere that isn't her
        status, res, err = run_task(orch, "constellation.open", {"url": page, "target": "tv"})
        assert status == "completed", err
        time.sleep(1)
        assert any(p["url"] == page for p in cdp(port, "/json/list"))                 # opened inside her shell, whole URL
        deadline = time.time() + 10                                  # it tells her as soon as its shell is up
        while time.time() < deadline:
            devs = run_task(orch, "constellation.devices", {"computers": True})[1]["paired"]
            if devs[0].get("shell") == "on":
                break
            time.sleep(0.5)
        assert devs[0]["shell"] == "on" and devs[0]["agent_version"]

        status, res, err = run_task(orch, "constellation.home", {"target": "tv"})
        assert status == "completed" and res["opened"]["what"] == "Closed 1 page", err
        time.sleep(1)
        urls = [p["url"] for p in cdp(port, "/json/list") if p["type"] == "page"]
        assert len(urls) == 1 and f"/view?screen={got['device_id']}" in urls[0]

        httpx.post(f"{orch}/devices/{got['device_id']}/revoke").raise_for_status()   # the screen goes with its node
        proc.wait(timeout=45)
        assert proc.returncode == 3
        assert sql("SELECT status FROM devices WHERE kind='screen'")[0]["status"] == "revoked"
        time.sleep(1)
        with pytest.raises(OSError):
            cdp(port, "/json/list")                                  # the kiosk closed with it
    finally:
        proc.kill()
        subprocess.run(["pkill", "-f", f"remote-debugging-port={port}"], capture_output=True)


def test_an_older_agent_updates_itself_through_the_guard(stack, tmp_path):
    orch = stack["orch"]
    got = paired_node(orch)
    served = (APP / "aurelia_node.py").read_bytes()
    import re
    current = re.search(rb'VERSION = "([^"]+)"', served).group(1).decode()
    old = served.replace(f'VERSION = "{current}"'.encode(), b'VERSION = "0.0.1"')
    home, app = home_for(tmp_path, orch, got, agent_bytes=old)
    env = env_for(home, AURELIA_HELLO_SECONDS="1", AURELIA_POLL_WAIT="1")
    first = subprocess.run([sys.executable, str(app / "aurelia_node_guard.py"), "run"], env=env, capture_output=True,
                           text=True, timeout=30)
    assert first.returncode == 75, first.stderr                     # updated itself; the service restarts it
    assert (app / "aurelia_node.py").read_bytes() == served and (app / "aurelia_node.py.prev").read_bytes() == old
    second = subprocess.Popen([sys.executable, str(app / "aurelia_node_guard.py"), "run"], env=env,
                              stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            st = json.loads((home / ".config" / "aurelia-node" / "update.json").read_text())
            if "to" not in st:
                break
            time.sleep(0.3)
        assert "to" not in st                                         # it reconnected: the update is done
        [row] = sql("SELECT info->>'agent_version' AS v FROM devices WHERE id=$1", got["device_id"])
        assert row["v"] == current
    finally:
        second.kill()
    assert f"updated from 0.0.1 to {current}" in second.stderr.read()


def test_three_failed_updates_reach_the_mailbox_and_retry_starts_over(stack, tmp_path, monkeypatch):
    from app import nodes
    orch = stack["orch"]
    got = paired_node(orch)
    monkeypatch.setattr(nodes, "agent_release", lambda: {"version": "9.9.9", "sha256": "0" * 64})   # a bad release
    home, app = home_for(tmp_path, orch, got)
    proc = subprocess.Popen([sys.executable, str(app / "aurelia_node_guard.py"), "run"],
                            env=env_for(home, AURELIA_HELLO_SECONDS="0.5", AURELIA_POLL_WAIT="1"),
                            stderr=subprocess.PIPE, text=True)
    try:
        deadline, items = time.time() + 20, []
        while time.time() < deadline and not items:
            items = [i for i in httpx.get(f"{orch}/mailbox").json()["items"] if i["kind"] == "node_update"]
            time.sleep(0.3)
        [card] = items
        assert card["title"] == "Living room TV couldn't update its agent" and "9.9.9" in card["summary"]
        assert "checksum" in card["evidence"][0]
        assert (app / "aurelia_node.py").read_bytes() == (APP / "aurelia_node.py").read_bytes()   # never swapped
        time.sleep(2)
        assert len([i for i in httpx.get(f"{orch}/mailbox").json()["items"] if i["kind"] == "node_update"]) == 1   # once
        out = httpx.post(f"{orch}/mailbox/{card['id']}/decide", json={"action": "approve"}).json()
        assert "try the update again" in out["done"]
        deadline, again = time.time() + 20, []
        while time.time() < deadline and not again:                    # it tried three more times, and says so again
            again = [i for i in httpx.get(f"{orch}/mailbox").json()["items"] if i["kind"] == "node_update"]
            time.sleep(0.3)
        assert again and again[0]["id"] != card["id"]
    finally:
        proc.kill()


def test_a_restarted_agent_takes_over_the_kiosk_left_from_before(stack, tmp_path, display):
    orch = stack["orch"]
    got = paired_node(orch)
    home, app = home_for(tmp_path, orch, got, shell=True)
    port = free_port()
    env = env_for(home, DISPLAY=display, XDG_CURRENT_DESKTOP="Aurelia", AURELIA_BROWSER=CHROME,
                  AURELIA_BROWSER_ARGS="--no-sandbox --disable-gpu", AURELIA_DEBUG_PORT=str(port), AURELIA_POLL_WAIT="2")

    def her_screen(timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                pages = [p for p in cdp(port, "/json/list") if p["type"] == "page"]
                if any(f"/view?screen={got['device_id']}" in p["url"] for p in pages):
                    return pages
            except OSError:
                pass
            time.sleep(0.5)
        return []

    first = subprocess.Popen([sys.executable, str(app / "aurelia_node_guard.py"), "run"], env=env, stderr=subprocess.DEVNULL)
    second = None
    try:
        assert her_screen()
        before = cdp(port, "/json/version")["webSocketDebuggerUrl"]
        first.kill()                                            # the agent goes; its kiosk stays open
        first.wait(10)
        second = subprocess.Popen([sys.executable, str(app / "aurelia_node_guard.py"), "run"], env=env, stderr=subprocess.PIPE,
                                  text=True)
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                if cdp(port, "/json/version")["webSocketDebuggerUrl"] != before and her_screen(2):
                    break
            except OSError:
                pass
            time.sleep(0.5)
        assert cdp(port, "/json/version")["webSocketDebuggerUrl"] != before       # a fresh kiosk, not the orphan
        assert her_screen()
        deadline = time.time() + 15                             # it says so at its next check-in
        while time.time() < deadline:
            devs = run_task(orch, "constellation.devices", {"computers": True})[1]["paired"]
            if devs[0].get("shell") == "on":
                break
            time.sleep(0.5)
        assert devs[0]["shell"] == "on"
    finally:
        first.kill()
        if second:
            second.kill()
        subprocess.run(["pkill", "-f", f"remote-debugging-port={port}"], capture_output=True)
