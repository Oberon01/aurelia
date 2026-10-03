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


@pytest.mark.parametrize("asked,tv", [("the tv", True), ("the living room TV", True), ("the big screen", True),
                                      ("the desk", False), ("my phone", False), ("tv and the desk", False)])
def test_the_tv_means_the_screen_showing_her(asked, tv):
    from app.constellation import _tvish
    assert _tvish(asked) is tv


# ---- step 2: Roku TVs ---------------------------------------------------------------------------------

class FakeJellyfin:
    playing: list = []

    def __init__(self, redis):
        pass

    async def search(self, q, kind="any", limit=20):
        lib = {"dune": {"Id": "m1", "Type": "Movie", "Name": "Dune", "ProductionYear": 2021}}
        return [lib[q.lower()]] if q.lower() in lib else []

    async def item(self, i):
        return {"Id": i, "Type": "Movie", "Name": "Dune"}

    async def sessions(self):
        return [{"Client": "Roku", "NowPlayingItem": n} for n in self.playing]


@pytest.fixture
def roku(monkeypatch):
    import types
    calls = []
    answers = {}

    async def orbit(app, user, tool, args, wait=45):
        calls.append((tool, args))
        a = answers.get(tool)
        if isinstance(a, Exception):
            raise a
        return a or {"name": "Living Room TV"}
    monkeypatch.setattr(quasar, "Jellyfin", FakeJellyfin)
    monkeypatch.setattr(quasar, "_orbit", orbit)
    monkeypatch.setattr(quasar, "ROKU_CONFIRM", 0.05)
    FakeJellyfin.playing = []
    cfg = types.SimpleNamespace(quasar=types.SimpleNamespace(queue_episodes=2))
    app = types.SimpleNamespace(state=types.SimpleNamespace(redis=None, config=types.SimpleNamespace(current=cfg)))
    return app, calls, answers


def run(coro):
    import asyncio
    return asyncio.run(coro)


def test_a_library_movie_opens_in_the_rokus_jellyfin_and_jellyfin_confirms_it(roku):
    app, calls, _ = roku
    FakeJellyfin.playing = [{"Id": "m1", "Type": "Movie", "Name": "Dune", "ProductionYear": 2021}]
    out = run(quasar.on_roku(app, "owner", "living room tv", "dune", {}))
    assert calls == [("tv_app", {"name": "living room tv", "app": "Jellyfin", "content_id": "m1", "media_type": "movie"})]
    assert out["headline"] == "Playing Dune (2021) on Living Room TV" and "unconfirmed" not in out["did"]


def test_jellyfin_not_listing_it_playing_is_not_called_playing(roku):
    app, calls, _ = roku
    out = run(quasar.on_roku(app, "owner", "tv", "dune", {}))
    assert out["headline"].startswith("Asked Living Room TV to play") and out["did"]["unconfirmed"] is True


def test_no_jellyfin_on_the_roku_falls_back_to_rokus_search(roku):
    app, calls, answers = roku
    answers["tv_app"] = ValueError("There's no app called 'Jellyfin' on the TV. Installed: Netflix.")
    answers["tv_play"] = {"name": "Living Room TV", "confirmed": True, "play": "playing", "in": "Netflix",
                          "message": "Playing Dune in Netflix on Living Room TV."}
    out = run(quasar.on_roku(app, "owner", "tv", "dune", {}))
    assert calls[-1] == ("tv_play", {"name": "tv", "title": "Dune", "kind": "movie"})
    assert out["headline"] == "Playing Dune on Living Room TV"


def test_a_named_service_skips_the_library_and_says_what_the_tv_shows(roku):
    app, calls, answers = roku
    answers["tv_play"] = {"name": "Living Room TV", "confirmed": False, "play": "found",
                          "message": "Living Room TV is showing Roku's search for The Bear (Hulu)..."}
    out = run(quasar.play(app, "owner", {"what": "The Bear", "on": "hulu on the living room tv", "season": 2}))
    assert calls == [("tv_play", {"name": "living room tv", "title": "The Bear", "app": "hulu", "kind": "show",
                                  "season": 2})]
    assert out["headline"] == "Found The Bear on Living Room TV" and out["did"]["unconfirmed"] is True


@pytest.mark.parametrize("on,want", [("netflix", ("netflix", "")), ("Netflix on the TV", ("netflix", "TV")),
                                     ("prime video on my bedroom tv", ("prime video", "bedroom tv")),
                                     ("the desk", (None, "the desk")), ("orient express", (None, "orient express"))])
def test_a_streaming_service_is_told_apart_from_a_tv(on, want):
    assert quasar.split_service(on) == want


def test_a_missing_governance_entry_is_said_plainly_not_as_a_missing_tv(monkeypatch):
    import types
    from fastapi import HTTPException
    from app import tasks

    async def launch(app, start):
        raise HTTPException(403, {"needs_confirmation": True, "tier": "change", "reason": "x"})
    monkeypatch.setattr(tasks, "launch", launch)
    with pytest.raises(ValueError, match="governance has it as 'change'") as e:
        run(quasar._orbit(types.SimpleNamespace(), "owner", "tv_play", {}))
    assert not isinstance(e.value, quasar.NoRoku)


def test_an_orbit_that_doesnt_know_the_tool_is_said_plainly(monkeypatch):
    import types
    from fastapi import HTTPException
    from app import tasks

    async def launch(app, start):
        raise HTTPException(502, "function 'orbit' did not accept the task: 404")
    monkeypatch.setattr(tasks, "launch", launch)
    with pytest.raises(ValueError, match="older version") as e:
        run(quasar._orbit(types.SimpleNamespace(), "owner", "tv_play", {}))
    assert not isinstance(e.value, quasar.NoRoku)


# ---- step 3: YouTube --------------------------------------------------------------------------------

def test_every_quasar_tool_is_one_it_runs():
    assert {t.name for t in quasar.TOOLS} == set(quasar.HANDLERS)


def test_the_yt_dlp_bundle_is_the_pinned_package_and_the_same_every_time():
    import io
    import zipfile
    from app import ytdlp_bundle
    ytdlp_bundle._cache = None
    a = ytdlp_bundle.build()
    ytdlp_bundle._cache = None
    b = ytdlp_bundle.build()
    assert a["sha256"] == b["sha256"] and a["version"].startswith("yt-dlp ")
    names = zipfile.ZipFile(io.BytesIO(a["zip"])).namelist()
    assert "yt_dlp/__main__.py" in names and any(n.startswith("yt_dlp_ejs/") for n in names)
    assert not any("__pycache__" in n or n.startswith("..") for n in names)


@pytest.fixture
def yt(monkeypatch):
    import types
    asked = []
    answers = {}

    async def ask(app, node, kind, args, timeout=25):
        asked.append((kind, args))
        return answers[kind](args) if callable(answers.get(kind)) else answers.get(kind, {})

    async def where(app, on):
        return {"id": 5, "name": "TV PC"}

    async def fetchval(*a):
        return '{"agent_version": "0.7.0"}'

    async def no_tv(app, user, node):
        return {}
    from app import constellation, db
    monkeypatch.setattr(constellation, "_ask_new", ask)
    monkeypatch.setattr(constellation, "show_on_tv", no_tv)
    monkeypatch.setattr(quasar, "_where", where)
    monkeypatch.setattr(db, "pool", lambda: types.SimpleNamespace(fetchval=fetchval))

    class R:
        async def get(self, k): return None
    app = types.SimpleNamespace(state=types.SimpleNamespace(redis=R()))
    return app, asked, answers


def test_his_youtube_feed_comes_back_as_posters(yt):
    app, asked, answers = yt
    answers["youtube_feed"] = {"signed_in": True, "items": [
        {"id": "dQw4w9WgXcQ", "title": "A song", "channel": "Rick", "seconds": 213, "date": "20091025"},
        {"id": "bad id", "title": "x"}]}
    out = run(quasar.youtube(app, "owner", {"feed": "subs"}))
    assert asked == [("youtube_feed", {"feed": "subscriptions", "query": "", "limit": 30})]
    assert out["headline"] == "Subscriptions · 1 video" and "note" not in out
    assert out["catalog"]["items"] == [{"id": "yt:dQw4w9WgXcQ", "name": "A song", "type": "video", "channel": "Rick",
                                        "art": "/quasar/ytart/dQw4w9WgXcQ", "minutes": 4, "date": "2009-10-25"}]


def test_a_feed_without_his_sign_in_says_so(yt):
    app, asked, answers = yt
    answers["youtube_feed"] = {"signed_in": False, "items": []}
    out = run(quasar.youtube(app, "owner", {}))
    assert asked[0][1]["feed"] == "home" and "isn't signed in to YouTube" in out["note"]


def test_a_tapped_video_plays_by_its_id_and_the_player_confirms_it(yt):
    app, asked, answers = yt
    answers["play"] = {"player": {"active": True, "item": "yt:dQw4w9WgXcQ", "title": "A song", "position": 0.4}}
    out = run(quasar.play(app, "owner", {"id": "yt:dQw4w9WgXcQ", "on": ""}))
    assert asked == [("play", {"items": [{"youtube": "dQw4w9WgXcQ", "title": ""}]})]
    assert out["headline"] == "Playing A song on TV PC" and out["now"]["art"] == "/quasar/ytart/dQw4w9WgXcQ"


def test_a_player_showing_something_else_is_not_called_playing(yt):
    app, asked, answers = yt
    answers["play"] = {"player": {"active": True, "item": "jf:abc", "title": "Dune"}}
    with pytest.raises(ValueError, match="didn't start it"):
        run(quasar.play(app, "owner", {"id": "yt:dQw4w9WgXcQ"}))


def test_play_x_on_youtube_plays_the_top_result_that_isnt_live(yt):
    app, asked, answers = yt
    answers["youtube_feed"] = {"items": [{"id": "jNQXAC9IVRw", "title": "Live now", "live": True},
                                         {"id": "dQw4w9WgXcQ", "title": "Lofi"}]}
    answers["play"] = {"player": {"active": True, "item": "yt:dQw4w9WgXcQ", "title": "Lofi"}}
    out = run(quasar.play(app, "owner", {"what": "lofi beats", "on": "youtube on the tv"}))
    assert asked[0] == ("youtube_feed", {"feed": "search", "query": "lofi beats", "limit": 5})
    assert asked[1] == ("play", {"items": [{"youtube": "dQw4w9WgXcQ", "title": "Lofi"}]})
    assert out["headline"] == "Playing Lofi on TV PC"


def test_no_nodejs_offers_to_install_it(yt):
    app, asked, answers = yt

    def fail(args):
        raise ValueError("YouTube needs Node.js here (Ubuntu's nodejs package, version 20 or newer)")
    answers["play"] = fail
    out = run(quasar.play(app, "owner", {"id": "yt:dQw4w9WgXcQ"}))
    assert out["confirm"]["tool"] == "constellation.install_package" and out["confirm"]["args"]["name"] == "nodejs"


def test_youtube_thumbnails_only_take_a_video_id():
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        run(quasar.ytart("../../etc"))
