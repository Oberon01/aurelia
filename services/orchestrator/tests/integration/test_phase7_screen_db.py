"""Phase 7 step 4, with real processes: Aurelia sees and uses the page on a screen running her
shell. A real agent keeps a real Chromium kiosk on a virtual display; she numbers what can be
clicked, clicks by number and by words, types, scrolls, goes back and controls a video, and
anything that would buy or send waits for his confirmation. The test page also tries to fool
her (it replaces the page's own DOM functions), which mustn't matter.

Skipped without Xvfb, Chromium or ffmpeg (for the test video)."""
import functools
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from .conftest import DB, REDIS
from .test_phase7_shell_db import CHROME, cdp, display, env_for, free_port, home_for, paired_node, run_task  # noqa: F401

pytestmark = [pytest.mark.skipif(not (DB and REDIS), reason="needs TEST_DATABASE_URL and TEST_REDIS_URL"),
              pytest.mark.skipif(not (CHROME and shutil.which("Xvfb") and shutil.which("ffmpeg")),
                                 reason="needs Xvfb, Chromium and ffmpeg")]
PAGE = Path(__file__).resolve().parent / "screen_page.html"


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    """A static server that can seek in a video (byte ranges), like any real site."""

    def log_message(self, *a):
        pass

    def send_head(self):
        path, rng = self.translate_path(self.path), self.headers.get("Range")
        if not rng or not os.path.isfile(path):
            return super().send_head()
        size = os.path.getsize(path)
        a, _, b = rng.split("=", 1)[1].partition("-")
        a, b = int(a or 0), (int(b) if b else size - 1)
        with open(path, "rb") as f:
            f.seek(a)
            data = f.read(b - a + 1)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        return io.BytesIO(data)


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    shutil.copy(PAGE, root / "index.html")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10", "-t", "60",
                    "-c:v", "libvpx", "-b:v", "60k", str(root / "clip.webm")], check=True, timeout=120)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(RangeHandler, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def ok(orch, tool, args):
    status, res, err = run_task(orch, tool, args, wait=30)
    assert status == "completed", err
    return res


def title(port):
    return next(p["title"] for p in cdp(port, "/json/list") if p["type"] == "page" and "127.0.0.1" in p["url"]
                and "/view" not in p["url"])


def test_she_sees_and_uses_the_page_on_her_screen(stack, tmp_path, display, site):
    orch = stack["orch"]
    got = paired_node(orch, "TV PC")
    home, app = home_for(tmp_path, orch, got, shell=True)
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(app / "aurelia_node_guard.py"), "run"], stderr=subprocess.PIPE, text=True,
                            env=env_for(home, DISPLAY=display, XDG_CURRENT_DESKTOP="Aurelia", AURELIA_BROWSER=CHROME,
                                        AURELIA_BROWSER_ARGS="--no-sandbox --disable-gpu", AURELIA_DEBUG_PORT=str(port),
                                        AURELIA_POLL_WAIT="2"))
    try:
        deadline = time.time() + 30                                   # her shell is up and says so
        while time.time() < deadline:
            devs = run_task(orch, "constellation.devices", {"computers": True})[1]["paired"]
            if devs and devs[0].get("shell") == "on":
                break
            time.sleep(0.5)
        assert devs[0]["shell"] == "on"

        err = run_task(orch, "constellation.look", {})[2]
        assert "nothing is open" in err                              # it's showing her; nothing to use

        ok(orch, "constellation.open", {"url": f"{site}/index.html"})
        time.sleep(1.5)

        # Look: numbered, on the screen too (a screenshot shows it), whatever the page did to its own DOM.
        seen = ok(orch, "constellation.look", {})                    # no name: the one screen running her shell
        items = {i["label"]: i["n"] for i in seen["onscreen"]["items"]}
        assert {"Search videos", "First video", "Second video", "Third video", "Place order", "Message"} <= set(items)
        assert [i["n"] for i in seen["onscreen"]["items"]] == list(range(1, len(items) + 1))
        assert seen["onscreen"]["video"]["duration"] == 60 and seen["onscreen"]["video"]["paused"] is True
        shot = httpx.get(orch + seen["onscreen"]["shot"])
        assert shot.status_code == 200 and shot.headers["content-type"] == "image/jpeg" and shot.content[:2] == b"\xff\xd8"
        assert httpx.get(orch + "/constellation/shots/" + "0" * 32 + ".jpg").status_code == 404

        # Click the second video, by its number.
        res = ok(orch, "constellation.click", {"n": items["Second video"], "target": "tv pc"})
        assert res["did"]["what"] == 'Clicked "Second video"' and title(port) == "Watching second video"

        # Buying waits for him: nothing pressed, a confirmation to hold instead.
        res = ok(orch, "constellation.click", {"text": "place order"})
        assert res["confirm"]["tool"] == "constellation.submit" and res["confirm"]["args"]["expect"] == "Place order"
        assert "Press it?" in res["confirm"]["reason"]
        assert title(port) == "Watching second video"
        r = httpx.post(f"{orch}/tasks", json={"tool": "constellation.submit", "args": res["confirm"]["args"]})
        assert r.status_code == 409                                  # submit is a change: governance asks him
        tid = httpx.post(f"{orch}/tasks", json={"tool": "constellation.submit", "args": res["confirm"]["args"],
                                                "confirmed": True}).json()["task_id"]
        deadline = time.time() + 20
        while httpx.get(f"{orch}/tasks/{tid}").json()["status"] not in ("completed", "failed") and time.time() < deadline:
            time.sleep(0.2)
        assert title(port) == "ORDER PLACED"

        # Type a search and press Enter (a search box: no confirmation).
        res = ok(orch, "constellation.type", {"text": "cats", "n": items["Search videos"], "enter": True})
        assert res["did"]["url"].endswith("#searched=cats")
        # A message box: typed, but Enter (sending) waits for him.
        res = ok(orch, "constellation.type", {"text": "hello mom", "n": items["Message"], "enter": True})
        assert res["confirm"]["args"] == {"target": "TV PC", "enter": True, "expect": "Message"}
        assert not title(port).startswith("SENT")
        tid = httpx.post(f"{orch}/tasks", json={"tool": "constellation.submit", "args": res["confirm"]["args"],
                                                "confirmed": True}).json()["task_id"]
        deadline = time.time() + 20
        while httpx.get(f"{orch}/tasks/{tid}").json()["status"] not in ("completed", "failed") and time.time() < deadline:
            time.sleep(0.2)
        assert title(port) == "SENT: hello mom"

        # Never a password box.
        status, _, err = run_task(orch, "constellation.type", {"text": "hunter2", "n": items["Password"]})
        assert status == "failed" and "password" in err

        # The video.
        res = ok(orch, "constellation.media", {"action": "play"})
        assert res["video"]["paused"] is False and res["headline"].startswith("Playing")
        res = ok(orch, "constellation.media", {"action": "forward", "amount": "20 seconds"})
        assert res["video"]["time"] >= 20
        before = res["video"]["time"]
        res = ok(orch, "constellation.media", {"action": "back", "seconds": 5})
        assert before - 7 <= res["video"]["time"] <= before - 3
        res = ok(orch, "constellation.media", {"action": "seek", "amount": "0:42"})
        assert 42 <= res["video"]["time"] <= 44 and res["headline"].startswith("Jumped · 0:4")
        res = ok(orch, "constellation.media", {"action": "pause"})
        assert res["video"]["paused"] is True and res["video"]["position"].endswith("/ 1:00")
        assert ok(orch, "constellation.media", {"action": "volume", "level": 30})["video"]["volume"] == 30
        assert ok(orch, "constellation.media", {"action": "speed", "rate": 1.5})["video"]["speed"] == 1.5
        assert ok(orch, "constellation.media", {"action": "fullscreen"})["video"]["fullscreen"] is True
        assert ok(orch, "constellation.media", {"action": "exit_fullscreen"})["video"]["fullscreen"] is False
        # Numbers from the last look still work after the video controls.
        assert ok(orch, "constellation.click", {"n": items["Third video"]})["did"]["what"] == 'Clicked "Third video"'

        # Scroll: new numbers for what's in view now.
        res = ok(orch, "constellation.scroll", {"direction": "bottom"})
        assert [i["label"] for i in res["onscreen"]["items"]] == ["Link at the bottom"]
        status, _, err = run_task(orch, "constellation.click", {"n": items["Second video"]})
        assert status == "failed" and "no number" in err                # old numbers don't click something else
        # Back.
        res = ok(orch, "constellation.back", {})
        assert res["did"]["what"].startswith("Back to")
        # A key.
        assert ok(orch, "constellation.press", {"key": "escape"})["did"]["what"] == "Pressed escape"

        # Home closes it; there's nothing to use again.
        ok(orch, "constellation.home", {})
        assert "nothing is open" in run_task(orch, "constellation.look", {})[2]
    finally:
        proc.kill()
        subprocess.run(["pkill", "-f", f"remote-debugging-port={port}"], capture_output=True)


LOGIN_PAGE = """<!doctype html><title>Sign in</title>
<form action="password.html"><input type="email" name="identifier" aria-label="Email or phone"><button>Next</button></form>"""
PASSWORD_PAGE = """<!doctype html><title>Password</title>
<form action="{back}/welcome.html"><input type="password" name="pw" aria-label="Enter your password"><button>Next</button></form>"""
WELCOME_PAGE = """<!doctype html><title>Welcome</title><p>Signed in.</p>"""


def test_she_signs_in_with_a_login_saved_on_the_computer_without_ever_seeing_it(stack, tmp_path, display, site):
    orch = stack["orch"]
    root = tmp_path / "site"
    (root / "login.html").write_text(LOGIN_PAGE)
    (root / "password.html").write_text(PASSWORD_PAGE.replace("{back}", site.replace("127.0.0.1", "localhost")))  # back to the site
    (root / "welcome.html").write_text(WELCOME_PAGE)
    secret = "s3cret-Aurelia-never-sees-this"
    got = paired_node(orch, "TV PC")
    home, app = home_for(tmp_path, orch, got, shell=True)
    cfg = home / ".config" / "aurelia-node"
    logins = {"testsite": {"label": "Test site", "origins": [site], "start": f"{site}/login.html",
                           "user": "me@example.com", "password": secret},
              "lookalike": {"label": "Lookalike", "origins": ["http://127.0.0.1:1"], "start": f"{site}/login.html",
                            "user": "me@example.com", "password": secret}}
    (cfg / "logins.json").write_text(json.dumps(logins))
    (cfg / "logins.json").chmod(0o600)
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(app / "aurelia_node_guard.py"), "run"], stderr=subprocess.PIPE, text=True,
                            env=env_for(home, DISPLAY=display, XDG_CURRENT_DESKTOP="Aurelia", AURELIA_BROWSER=CHROME,
                                        AURELIA_BROWSER_ARGS="--no-sandbox --disable-gpu", AURELIA_DEBUG_PORT=str(port),
                                        AURELIA_POLL_WAIT="2"))
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            devs = run_task(orch, "constellation.devices", {"computers": True})[1]["paired"]
            if devs and devs[0].get("shell") == "on":
                break
            time.sleep(0.5)
        names = ok(orch, "constellation.logins", {"target": "tv pc"})
        assert "Test site" in names["did"]["what"] and secret not in json.dumps(names) and "me@example" not in json.dumps(names)

        r = httpx.post(f"{orch}/tasks", json={"tool": "constellation.sign_in", "args": {"target": "tv pc", "site": "testsite"}})
        assert r.status_code == 409                                                      # always his confirmation
        tid = httpx.post(f"{orch}/tasks", json={"tool": "constellation.sign_in", "args": {"target": "tv pc", "site": "testsite"},
                                                "confirmed": True}).json()["task_id"]
        deadline = time.time() + 60
        while time.time() < deadline and httpx.get(f"{orch}/tasks/{tid}").json()["status"] not in ("completed", "failed"):
            time.sleep(0.3)
        t = httpx.get(f"{orch}/tasks/{tid}").json()
        assert t["status"] == "completed", t.get("error")
        res = t["result"] if isinstance(t["result"], dict) else json.loads(t["result"])
        assert res["headline"] == "Signed in to Test site"
        page = next(p for p in cdp(port, "/json/list") if p["type"] == "page" and "/view" not in p["url"])
        assert page["title"] == "Welcome" and "pw=" + secret in page["url"]              # the site got the password
        import asyncio, asyncpg
        async def everything():
            c = await asyncpg.connect(DB)
            try:
                return json.dumps([dict(r) for r in await c.fetch("SELECT result, args, error FROM task_runs")], default=str)
            finally:
                await c.close()
        stored = asyncio.run(everything())
        assert secret not in stored and "me@example.com" not in stored                   # Aurelia never had it

        tid = httpx.post(f"{orch}/tasks", json={"tool": "constellation.sign_in", "args": {"target": "tv pc", "site": "lookalike"},
                                                "confirmed": True}).json()["task_id"]
        deadline = time.time() + 60
        while time.time() < deadline and httpx.get(f"{orch}/tasks/{tid}").json()["status"] not in ("completed", "failed"):
            time.sleep(0.3)
        t = httpx.get(f"{orch}/tasks/{tid}").json()
        assert t["status"] == "failed" and "isn't Lookalike's sign-in page" in t["error"]
        page = next(p for p in cdp(port, "/json/list") if p["type"] == "page" and "/view" not in p["url"] and "login" in p["url"])
        assert page["title"] == "Sign in"                                                # nothing filled or submitted

        status, _, err = run_task(orch, "constellation.type", {"text": secret, "n": 1})
        assert status == "failed"                                                        # she still types no passwords herself
    finally:
        proc.kill()
        subprocess.run(["pkill", "-f", f"remote-debugging-port={port}"], capture_output=True)
    assert secret not in proc.stderr.read()                                              # nor in the agent's log
