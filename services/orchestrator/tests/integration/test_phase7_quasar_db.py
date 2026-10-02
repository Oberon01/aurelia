"""Quasar with real processes: a real node agent plays from a (stand-in) Jellyfin with her own
player (real mpv, with no picture or sound). She finds things in his library, plays a show where
he left off and keeps going, controls it, and Jellyfin hears where he got to. The stream reaches the
computer only through Aurelia, only with that computer's key; Jellyfin's sign-in never leaves the
server. Skipped without mpv or ffmpeg."""
import functools
import http.server
import json
import shutil
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

import httpx
import pytest

from .conftest import DB, REDIS
from .test_phase7_nodes_db import node, run_task  # noqa: F401

pytestmark = [pytest.mark.skipif(not (DB and REDIS), reason="needs TEST_DATABASE_URL and TEST_REDIS_URL"),
              pytest.mark.skipif(not (shutil.which("mpv") and shutil.which("ffmpeg")), reason="needs mpv and ffmpeg")]
TICKS = 10_000_000


def ep(i, n, played=False, resume=0):
    return {"Id": f"e{i}", "Type": "Episode", "Name": n, "SeriesName": "Frieren", "SeriesId": "s1",
            "ParentIndexNumber": 1, "IndexNumber": i, "RunTimeTicks": 30 * TICKS,
            "UserData": {"Played": played, "PlaybackPositionTicks": resume * TICKS}}


LIBRARY = [
    {"Id": "s1", "Type": "Series", "Name": "Frieren", "ProductionYear": 2023, "Overview": "After the journey."},
    ep(1, "The Journey's End", played=True), ep(2, "It Didn't Have to Be Magic", resume=6), ep(3, "Killing Magic"),
    {"Id": "m1", "Type": "Movie", "Name": "Big Buck", "ProductionYear": 2008, "RunTimeTicks": 30 * TICKS},
    {"Id": "al1", "Type": "MusicAlbum", "Name": "Discovery", "AlbumArtist": "Daft Punk"},
    {"Id": "t1", "Type": "Audio", "Name": "One More Time", "AlbumArtist": "Daft Punk", "Album": "Discovery", "ParentId": "al1",
     "IndexNumber": 1},
    {"Id": "t2", "Type": "Audio", "Name": "Aerodynamic", "AlbumArtist": "Daft Punk", "Album": "Discovery", "ParentId": "al1",
     "IndexNumber": 2},
]


class Jellyfin(http.server.BaseHTTPRequestHandler):
    media: dict = {}
    heard: list = []

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _signed_in(self):
        return 'Token="tok-123"' in (self.headers.get("Authorization") or "")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/Users/AuthenticateByName":
            if body != {"Username": "kentrell", "Pw": "pw"}:
                return self._json({}, 401)
            return self._json({"AccessToken": "tok-123", "User": {"Id": "u1"}})
        if not self._signed_in():
            return self._json({}, 401)
        Jellyfin.heard.append((self.path, body))
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/System/Info/Public":
            return self._json({"ServerName": "atlas-ai", "Version": "10.11.0"})
        if not self._signed_in():
            return self._json({}, 401)
        if u.path == "/Items":
            items = LIBRARY
            if q.get("ids"):
                items = [i for i in items if i["Id"] in q["ids"].split(",")]
            if q.get("searchTerm"):
                items = [i for i in items if q["searchTerm"].lower() in (i["Name"] + i.get("SeriesName", "")).lower()]
            if q.get("includeItemTypes"):
                items = [i for i in items if i["Type"] in q["includeItemTypes"].split(",")]
            if q.get("parentId"):
                items = [i for i in items if i.get("ParentId") == q["parentId"]]
            if q.get("filters") == "IsResumable":
                items = [i for i in items if (i.get("UserData") or {}).get("PlaybackPositionTicks")]
            return self._json({"Items": items, "TotalRecordCount": len(items)})
        if u.path == "/Shows/s1/Episodes":
            return self._json({"Items": [i for i in LIBRARY if i.get("SeriesId") == "s1"]})
        if u.path == "/Shows/NextUp":
            return self._json({"Items": [i for i in LIBRARY if i["Id"] == "e2"] if q.get("seriesId") in ("s1", None) else []})
        if u.path.endswith("/stream"):
            data = Jellyfin.media["audio" if u.path.startswith("/Audio") else "video"]
            rng = self.headers.get("Range")
            start, end = 0, len(data) - 1
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                start, end = int(a or 0), int(b) if b else len(data) - 1
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", "audio/mpeg" if u.path.startswith("/Audio") else "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            self.end_headers()
            try:
                self.wfile.write(data[start:end + 1])
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if u.path.endswith("/Images/Primary"):
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(b"\xff\xd8\xff\xe0poster")
            return
        self._json({}, 404)


@pytest.fixture
def jellyfin(tmp_path, monkeypatch):
    v, a = tmp_path / "v.mp4", tmp_path / "a.mp3"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10", "-f", "lavfi",
                    "-i", "sine=frequency=440", "-t", "30", "-c:v", "libx264", "-g", "10", "-c:a", "aac", "-shortest",
                    "-movflags", "+faststart", str(v)], check=True, timeout=120)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330", "-t", "20",
                    "-c:a", "libmp3lame", str(a)], check=True, timeout=60)
    Jellyfin.media = {"video": v.read_bytes(), "audio": a.read_bytes()}
    Jellyfin.heard = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Jellyfin)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("JELLYFIN_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setenv("JELLYFIN_USER", "kentrell")
    monkeypatch.setenv("JELLYFIN_PASSWORD", "pw")
    import redis as redis_sync
    redis_sync.Redis.from_url(REDIS).delete("aurelia:jellyfin:auth")
    yield Jellyfin
    srv.shutdown()


def ok(orch, tool, args, wait=40):
    status, res, err = run_task(orch, tool, args, wait=wait)
    assert status == "completed", err
    return res


def heard(j, path, item, wait=15):
    deadline = time.time() + wait
    while time.time() < deadline:
        hits = [b for p, b in j.heard if p == path and b.get("ItemId") == item]
        if hits:
            return hits
        time.sleep(0.3)
    raise AssertionError(f"Jellyfin never heard {path} for {item}: {j.heard}")


def test_she_plays_his_library_on_a_computer_with_her_own_player(node, jellyfin):
    orch = node["orch"]

    assert httpx.get(f"{orch}/quasar/check", timeout=30).json() == {"ok": True, "server": "atlas-ai", "version": "10.11.0",
                                                                  "signed_in_as": "kentrell"}
    lib = ok(orch, "quasar.find", {})
    groups = {g["name"]: [i["name"] for i in g["items"]] for g in lib["catalog"]["groups"]}
    assert groups["Continue"] == ["It Didn't Have to Be Magic"] and "Frieren" in str(lib)
    found = ok(orch, "quasar.find", {"query": "discovery"})["catalog"]["items"]
    assert found[0]["type"] == "album" and found[0]["artist"] == "Daft Punk" and found[0]["art"] == "/quasar/art/al1"

    # A show: next up, where he left off, and the rest of the season queued.
    res = ok(orch, "quasar.play", {"what": "frieren", "on": "living room tv"})
    now = res["now"]
    assert res["headline"] == "Playing Frieren · S1E2 · It Didn't Have to Be Magic on Living room TV", res
    assert now["active"] and now["position"] >= 5 and now["next"] == "Frieren · S1E3 · Killing Magic" and now["count"] == 2
    assert now["resumed"] and now["art"] == "/quasar/art/s1"
    start = heard(jellyfin, "/Sessions/Playing", "e2")[0]
    assert start["PositionTicks"] >= 5 * TICKS and start["PlayMethod"] == "DirectPlay"

    # Control, from the player itself (the computer has no shell: everything goes to the player).
    assert ok(orch, "constellation.media", {"action": "pause"})["headline"].startswith("Paused · 0:")
    assert ok(orch, "constellation.media", {"action": "now_playing"})["headline"].startswith(
        "Paused: Frieren · S1E2 · It Didn't Have to Be Magic (0:")
    assert ok(orch, "constellation.media", {"action": "forward", "amount": "5 seconds"})["now"]["position"] >= 10
    nxt = ok(orch, "constellation.media", {"action": "next"})
    assert nxt["headline"] == "Next · Frieren · S1E3 · Killing Magic" and nxt["now"]["index"] == 1
    assert heard(jellyfin, "/Sessions/Playing/Stopped", "e2")[0]["PositionTicks"] >= 10 * TICKS   # where he got to
    heard(jellyfin, "/Sessions/Playing", "e3")
    status, _, err = run_task(orch, "constellation.media", {"action": "subtitles", "which": "on"})
    assert status == "failed" and "no subtitles" in err
    stopped = ok(orch, "constellation.media", {"action": "stop"})
    assert stopped["headline"] == "Stopped Frieren · S1E3 · Killing Magic" and stopped["now"]["active"] is False
    heard(jellyfin, "/Sessions/Playing/Stopped", "e3")
    assert ok(orch, "constellation.media", {"action": "now_playing"})["headline"] == "Nothing is playing on Living room TV"

    # Music: the album, in order; a show by season and episode from the start.
    res = ok(orch, "quasar.play", {"what": "discovery"})
    assert res["now"]["kind"] == "audio" and res["now"]["title"] == "Daft Punk — One More Time" and res["now"]["count"] == 2
    res = ok(orch, "quasar.play", {"what": "frieren", "season": 1, "episode": 1, "from_start": True})
    assert res["now"]["title"].startswith("Frieren · S1E1") and res["now"]["position"] < 3 and "resumed" not in res["now"]
    ok(orch, "constellation.media", {"action": "stop"})

    status, _, err = run_task(orch, "quasar.play", {"what": "nonexistent thing"})
    assert status == "failed" and "couldn't find" in err

    # The stream: only through Aurelia, only with this computer's key, only for this computer.
    import redis as redis_sync
    r = redis_sync.Redis.from_url(REDIS)
    ticket = next(k.decode().rsplit(":", 1)[1] for k in r.scan_iter("aurelia:media:ticket:*"))
    assert httpx.get(f"{orch}/nodes/media/{ticket}").status_code in (401, 403)
    mine = httpx.get(f"{orch}/nodes/media/{ticket}", headers={"Authorization": f"Bearer {node['key']}", "Range": "bytes=0-99"})
    assert mine.status_code == 206 and len(mine.content) == 100 and mine.headers["content-range"].startswith("bytes 0-99/")
    assert httpx.get(f"{orch}/nodes/media/{'0' * 32}", headers={"Authorization": f"Bearer {node['key']}"}).status_code == 404
    assert httpx.get(f"{orch}/quasar/art/s1").content.startswith(b"\xff\xd8")
    import asyncio, asyncpg
    async def stored():
        c = await asyncpg.connect(DB)
        try:
            return json.dumps([dict(x) for x in await c.fetch("SELECT args, result, error FROM task_runs")], default=str)
        finally:
            await c.close()
    assert "tok-123" not in asyncio.run(stored()) and "kentrell" not in asyncio.run(stored())   # Jellyfin's sign-in stays put


def test_without_jellyfin_she_says_what_to_set_up(stack, monkeypatch):
    for k in ("JELLYFIN_URL", "JELLYFIN_USER", "JELLYFIN_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    status, _, err = run_task(stack["orch"], "quasar.find", {"query": "x"})
    assert status == "failed" and "JELLYFIN_URL" in err
    assert "JELLYFIN_URL" in httpx.get(f"{stack['orch']}/quasar/check").json()["problem"]
