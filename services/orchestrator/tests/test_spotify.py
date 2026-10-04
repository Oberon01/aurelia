"""Spotify (Quasar step 4): signing in with PKCE (no secret), refreshing, choosing what and where,
and that every change is checked with what Spotify then reports."""
import asyncio
import json
import os
import types
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")
os.environ.setdefault("REDIS_URL", "redis://unused:6379/0")

import fakeredis.aioredis  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402

from app import quasar, spotify  # noqa: E402


def run(c):
    return asyncio.run(c)


class Accounts:
    def __init__(self):
        self.forms = []
        self.refresh_ok = True

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/token":
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            self.forms.append(form)
            if form["grant_type"] == "refresh_token" and not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": f"A{len(self.forms) + 1}", "expires_in": 3600,
                                             **({"refresh_token": "R1"} if form["grant_type"] == "authorization_code" else {})})
        if req.url.path == "/v1/me/player/devices":
            if req.headers["authorization"] == "Bearer A1":
                return httpx.Response(401)                    # an old token: refreshed once, then fine
            return httpx.Response(200, json={"devices": [{"id": "d1", "name": "TV PC"}]})
        return httpx.Response(404)


def make(acc):
    r = fakeredis.aioredis.FakeRedis()
    return spotify.Spotify(r, "cid", "https://aura.example/quasar/spotify/callback", httpx.MockTransport(acc.handler)), r


def test_sign_in_uses_pkce_and_a_one_time_state_and_never_a_secret():
    acc = Accounts()
    sp, r = make(acc)
    url = run(sp.login_url())
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert url.startswith("https://accounts.spotify.com/authorize?") and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == "https://aura.example/quasar/spotify/callback" and "client_secret" not in url
    run(sp.finish("CODE", q["state"]))
    form = acc.forms[0]
    assert form["grant_type"] == "authorization_code" and form["client_id"] == "cid" and "client_secret" not in form
    assert spotify._challenge(form["code_verifier"]) == q["code_challenge"]
    with pytest.raises(ValueError, match="already used or expired"):
        run(sp.finish("CODE", q["state"]))                     # once only
    assert run(sp.signed_in()) is True


def test_an_expired_token_is_refreshed_and_a_refused_refresh_signs_out():
    acc = Accounts()
    sp, r = make(acc)
    run(r.set(spotify.AUTH_KEY, json.dumps({"access": "A1", "refresh": "R1", "expires": 9e12})))
    assert run(sp.devices())[0]["name"] == "TV PC"            # 401 → refreshed → retried
    assert acc.forms[-1]["grant_type"] == "refresh_token"
    acc.refresh_ok = False
    run(r.set(spotify.AUTH_KEY, json.dumps({"access": "A9", "refresh": "R1", "expires": 0})))
    with pytest.raises(spotify.NotSignedIn, match="signed me out"):
        run(sp.devices())
    assert run(r.get(spotify.AUTH_KEY)) is None


def test_not_set_up_says_where():
    with pytest.raises(spotify.NotSignedIn, match="spotify_client_id"):
        spotify.Spotify(None, "", "")


def test_what_he_means_his_playlists_first_then_exact_names():
    found = {"tracks": {"items": [{"name": "Chill Vibes (Remix)", "uri": "spotify:track:t1"}]},
             "artists": {"items": [{"name": "Daft Punk", "uri": "spotify:artist:a1"}]},
             "albums": {"items": [{"name": "Discovery", "uri": "spotify:album:b1", "artists": [{"name": "Daft Punk"}]}]},
             "playlists": {"items": [None, {"name": "Chill Vibes", "uri": "spotify:playlist:p9"}]}}
    mine = [{"name": "Chill Vibes", "uri": "spotify:playlist:mine"}]
    assert spotify.pick(found, "chill vibes", None, mine)["uri"] == "spotify:playlist:mine"
    assert spotify.pick(found, "daft punk", None, [])["type"] == "artist"
    assert spotify.pick(found, "discovery", "album", [])["uri"] == "spotify:album:b1"
    assert spotify.pick(found, "something else", None, [])["type"] == "track"          # Spotify's top hit
    assert spotify.label(spotify.pick(found, "discovery", "album", [])) == "the album Discovery by Daft Punk"


def test_a_speaker_by_what_he_calls_it():
    devs = [{"id": "1", "name": "TV PC", "is_active": False}, {"id": "2", "name": "Kentrell's iPhone", "is_active": True}]
    assert spotify.device_for(devs, "the tv")["id"] == "1" and spotify.device_for(devs, "")["id"] == "2"
    assert spotify.device_for(devs, "kitchen") is None


# ---- the tool, checked with Spotify ---------------------------------------------------------------

class FakeSp:
    def __init__(self, land=True):
        self.calls, self.land, self.st = [], land, {}
        self.devs = [{"id": "d1", "name": "TV PC"}]

    async def devices(self): return self.devs
    async def signed_in(self): return True
    async def my_playlists(self): return []

    async def search(self, q, types, limit=5):
        return {"albums": {"items": [{"name": "Discovery", "uri": "spotify:album:b1", "artists": [{"name": "Daft Punk"}]}]}}

    async def call(self, method, path, params=None, body=None):
        self.calls.append((method, path, params, body))
        if path == "/me/player/play" and self.land:
            self.st = {"device": {"id": params["device_id"], "name": "TV PC"}, "is_playing": True,
                       "context": {"uri": body["context_uri"]} if body and "context_uri" in body else None,
                       "item": {"uri": "spotify:track:x", "name": "One More Time", "type": "track", "duration_ms": 320000,
                                "artists": [{"name": "Daft Punk"}], "album": {"images": [{"url": "https://i.scdn.co/x"}]}}}
        if path == "/me/player/pause" and self.land:
            self.st = {**self.st, "is_playing": False}

    async def state(self): return self.st


@pytest.fixture
def app_with(monkeypatch):
    def make_app(sp):
        cfg = types.SimpleNamespace(quasar=types.SimpleNamespace(spotify_confirm_seconds=0.05, librespot_version="0.8.0"))
        app = types.SimpleNamespace(state=types.SimpleNamespace(redis=None, config=types.SimpleNamespace(current=cfg)))
        monkeypatch.setattr(quasar, "_sp", lambda a: sp)
        return app
    return make_app


def test_play_an_album_on_the_tv_and_spotify_confirms_it(app_with):
    sp = FakeSp()
    out = run(quasar.spotify_tool(app_with(sp), "owner", {"action": "play", "what": "Discovery", "kind": "album", "on": "the tv"}))
    assert sp.calls[0] == ("PUT", "/me/player/play", {"device_id": "d1"}, {"context_uri": "spotify:album:b1"})
    assert out["headline"] == "Playing the album Discovery by Daft Punk on TV PC"
    assert out["now"]["title"] == "One More Time by Daft Punk" and out["now"]["art"] == "https://i.scdn.co/x"


def test_spotify_not_reporting_it_is_not_called_playing(app_with):
    sp = FakeSp(land=False)
    out = run(quasar.spotify_tool(app_with(sp), "owner", {"action": "play", "what": "Discovery", "on": "tv pc"}))
    assert out["headline"].startswith("Asked Spotify to play") and out["did"]["unconfirmed"] is True


def test_pause_is_checked_too(app_with):
    sp = FakeSp()
    run(quasar.spotify_tool(app_with(sp), "owner", {"action": "play", "what": "Discovery", "on": "tv"}))
    out = run(quasar.spotify_tool(app_with(sp), "owner", {"action": "pause"}))
    assert out["headline"] == "Paused Spotify on TV PC" and out["now"]["paused"] is True


def test_a_computer_without_its_speaker_is_asked_to_start_it_or_offered_librespot(app_with, monkeypatch):
    from app import constellation
    sp = FakeSp()
    sp.devs = []
    asked = []

    async def where(app, on): return {"id": 3, "name": "Desk"}

    async def ask(app, node, kind, args, timeout=25):
        asked.append((kind, args))
        raise ValueError("librespot isn't installed here (it makes this computer a Spotify speaker)")
    monkeypatch.setattr(quasar, "_where", where)
    monkeypatch.setattr(constellation, "_ask_new", ask)
    out = run(quasar.spotify_tool(app_with(sp), "owner", {"action": "play", "what": "Discovery", "on": "desk"}))
    assert asked == [("spotify_speaker", {"name": "Desk"})]
    assert out["confirm"]["tool"] == "quasar.speaker" and out["confirm"]["args"] == {"target": "Desk"}
    assert "cargo" in out["confirm"]["reason"] and "0.8.0" in out["confirm"]["reason"]


def test_play_x_on_spotify_reaches_spotify(app_with, monkeypatch):
    sp = FakeSp()
    out = run(quasar.play(app_with(sp), "owner", {"what": "Discovery", "on": "spotify on the tv"}))
    assert sp.calls[0][2] == {"device_id": "d1"} and out["headline"].startswith("Playing the album Discovery")


def test_pause_on_a_computer_playing_spotify_pauses_spotify_there(app_with, monkeypatch):
    from app import constellation
    sp = FakeSp()
    app = app_with(sp)
    run(quasar.spotify_tool(app, "owner", {"action": "play", "what": "Discovery", "on": "tv"}))

    async def ask(app, node, kind, args, timeout=25):
        return {"players": []}                                     # nothing local: librespot has no MPRIS
    monkeypatch.setattr(constellation, "_ask_new", ask)
    out = run(constellation._player(app, {"id": 1, "name": "TV PC"}, "pause", {}))
    assert out["headline"] == "Paused Spotify on TV PC"
    sp.st["device"] = {"id": "d2", "name": "Kentrell's iPhone"}
    out = run(constellation._player(app, {"id": 1, "name": "TV PC"}, "pause", {}))
    assert out["headline"] == "Nothing is playing on TV PC"            # Spotify is on his phone, not here



def test_building_the_speaker_is_watched_to_the_end_and_he_hears_how_it_went(app_with, monkeypatch):
    from app import constellation
    told, states = [], iter([{"state": "building"}, {"state": "done", "version": "0.8.0", "running": True}])

    async def target(app, name, screen=False): return {"id": 3, "name": "TV PC"}

    async def ask(app, node, kind, args, timeout=25):
        if kind == "speaker_build":
            assert args == {"confirmed": True, "version": "0.8.0", "name": "TV PC"}
            return {"state": "installing", "step": "installing cargo from Ubuntu's packages"}
        return next(states)

    async def tell(app, user, display, head, text): told.append((display, head, text))

    async def asker(task): return 42
    monkeypatch.setattr(constellation, "target", target)
    monkeypatch.setattr(constellation, "_ask_new", ask)
    monkeypatch.setattr(constellation, "_tell", tell)
    monkeypatch.setattr(constellation, "_asker", asker)
    from app import engine
    queued = []

    async def after(app, seconds, name, spec, title=None):        # the watch is a job on Admiral's engine
        queued.append((seconds, name, spec))
        return 1
    monkeypatch.setattr(engine, "after", after)

    async def go():
        app = app_with(FakeSp())
        out = await quasar.speaker(app, "owner", {"target": "tv pc", "_self": "t"})
        (secs, name, spec), = queued
        assert name == "quasar.build_watch" and secs == quasar.BUILD_LOOK_EVERY
        step = engine.SYSTEM[name][0]
        nxt = await step(app, spec)                               # still building: look again later
        assert nxt["again_in"] == quasar.BUILD_LOOK_EVERY
        assert await step(app, nxt["spec"]) is None               # done: said, and the watch ends
        return out
    out = run(go())
    assert out["headline"] == "Building librespot on TV PC" and "I'll tell you" in out["did"]["what"]
    assert told and told[0][0] == 42 and told[0][1] == "TV PC is a Spotify speaker" and "pick it in the Spotify app" in told[0][2]
