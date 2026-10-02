"""Quasar's library logic on its own: which item a request means, and how items read."""
import os

os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")
os.environ.setdefault("REDIS_URL", "redis://unused:6379/0")

import pytest  # noqa: E402

from app import quasar  # noqa: E402

TICKS = quasar.TICKS


def test_the_show_wins_over_its_episodes_and_exact_names_win():
    hits = [{"Id": "e1", "Type": "Episode", "Name": "Frieren"}, {"Id": "s1", "Type": "Series", "Name": "Frieren"},
            {"Id": "m1", "Type": "Movie", "Name": "Frieren: The Movie"}]
    assert quasar.best(hits, "frieren")["Id"] == "s1"
    assert quasar.best(hits, "Frieren the movie")["Id"] == "m1"
    assert quasar.best([], "x") is None


def test_items_read_the_way_he_would_say_them():
    ep = {"Id": "e2", "Type": "Episode", "Name": "Magic", "SeriesName": "Frieren", "SeriesId": "s1",
          "ParentIndexNumber": 1, "IndexNumber": 2, "RunTimeTicks": 24 * 60 * TICKS,
          "UserData": {"PlaybackPositionTicks": 6 * 60 * TICKS}}
    assert quasar.label(ep) == "Frieren · S1E2 · Magic"
    s = quasar.shape(ep)
    assert s["type"] == "episode" and s["minutes"] == 24 and s["resume_minutes"] == 6 and s["art"] == "/quasar/art/s1"
    song = {"Id": "t1", "Type": "Audio", "Name": "One More Time", "AlbumArtist": "Daft Punk", "Album": "Discovery"}
    assert quasar.label(song) == "Daft Punk — One More Time" and quasar.shape(song)["album"] == "Discovery"
    assert quasar.label({"Type": "Movie", "Name": "Big Buck", "ProductionYear": 2008}) == "Big Buck (2008)"


def test_jellyfin_needs_setting_up_first():
    with pytest.raises(quasar.NotSetUp, match="JELLYFIN_URL"):
        quasar.Jellyfin(None, url="", user="", password="")


def test_the_panel_gets_what_the_player_said():
    st = {"active": True, "title": "Frieren · S1E2", "item": "jf:e2", "position": 61.0, "duration": 1440.0, "paused": False,
          "index": 0, "count": 2, "next": "Frieren · S1E3"}
    b = quasar.now_block("TV PC", st, {"e2": {"SeriesId": "s1"}})
    assert b["art"] == "/quasar/art/s1" and b["next"] == "Frieren · S1E3" and b["node"] == "TV PC"
    assert "art" not in quasar.now_block("TV PC", {**st, "item": "yt:abc"})


def test_she_waits_while_jellyfin_wakes_behind_lazytainer(monkeypatch):
    import asyncio
    import httpx
    tries = []

    def handler(req):
        tries.append(1)
        if len(tries) < 3:
            raise httpx.ConnectError("refused", request=req)       # Lazytainer is still starting it
        if len(tries) == 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ServerName": "atlas-ai", "Version": "10.11.0"})
    jf = quasar.Jellyfin(None, url="http://lazytainer-b:8096", user="k", password="p", wake_seconds=30)
    jf.retry_every = 0

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            r = await jf.call(c, "GET", f"{jf.url}/System/Info/Public")
            return r.json()
    assert asyncio.run(go())["ServerName"] == "atlas-ai" and len(tries) == 4


def test_she_gives_up_with_a_useful_reason():
    import asyncio
    import httpx
    jf = quasar.Jellyfin(None, url="http://nowhere:8096", user="k", password="p", wake_seconds=0)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: (_ for _ in ()).throw(httpx.ConnectError("refused", request=req)))) as c:
            await jf.call(c, "GET", f"{jf.url}/System/Info/Public")
    with pytest.raises(ValueError, match="home-lab network"):
        asyncio.run(go())
