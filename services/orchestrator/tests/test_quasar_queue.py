"""Quasar's queue (Phase 12.5) on its own: what's lined up, what's suggested near the end of a video, what plays
when it ends (the queue, then autoplay), and what doesn't (he stopped it, it didn't reach the end, autoplay is off
or ran too long). The player and YouTube are stand-ins; Redis is a fake."""
import asyncio
import json
import os
import time
import types

os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")
os.environ.setdefault("REDIS_URL", "redis://unused:6379/0")

import fakeredis.aioredis  # noqa: E402
import pytest  # noqa: E402

from app import constellation, events, quasar, quasar_queue as Q  # noqa: E402
from app.config import QuasarConfig  # noqa: E402

NODE = {"id": 5, "name": "TV PC"}
A, B, C = "yt:aaaaaaaaaaa", "yt:bbbbbbbbbbb", "yt:ccccccccccc"
JF = "jf:0123456789abcdef0123456789abcdef"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def q(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis()
    cfg = QuasarConfig()
    app = types.SimpleNamespace(state=types.SimpleNamespace(
        redis=redis, config=types.SimpleNamespace(current=types.SimpleNamespace(quasar=cfg))))
    played, panels, feed_asks = [], [], []
    feed = {"items": [{"id": "yt:aaaaaaaaaaa", "name": "Now playing", "type": "video", "art": "/quasar/ytart/aaaaaaaaaaa"},
                      {"id": "yt:ddddddddddd", "name": "Live thing", "type": "video", "live": True},
                      {"id": "yt:eeeeeeeeeee", "name": "Top pick", "type": "video", "art": "/quasar/ytart/eeeeeeeeeee"},
                      {"id": "yt:fffffffffff", "name": "Second", "type": "video"}]}

    async def where(app_, on):
        return dict(NODE)

    async def play_youtube(app_, user, node, vid, title=""):
        played.append(f"yt:{vid}")
        await redis.set(quasar.STATE.format(node["id"]), json.dumps({"active": True, "item": f"yt:{vid}", "title": title}))
        return {"headline": f"Playing {title or vid} on {node['name']}", "now": {"active": True, "title": title}}

    async def play(app_, user, args):
        played.append(f"jf:{args['id']}")
        assert args.get("_one") and args.get("_auto")                   # just that one, and not "he chose"
        return {"headline": "Playing a library item on TV PC", "now": {"active": True}}

    async def youtube(app_, user, args):
        feed_asks.append(args)
        return {"headline": "Your YouTube", "catalog": {"source": "youtube", "node": "TV PC", "items": feed["items"]}}

    async def publish(redis_, ev):
        panels.append(ev)

    monkeypatch.setattr(quasar, "_where", where)
    monkeypatch.setattr(quasar, "play_youtube", play_youtube)
    monkeypatch.setattr(quasar, "play", play)
    monkeypatch.setattr(quasar, "youtube", youtube)
    monkeypatch.setattr(events, "publish", publish)

    async def fetchval(sql, *a):
        return "TV PC" if "SELECT name FROM devices" in sql else None
    from app import db
    monkeypatch.setattr(db, "pool", lambda: types.SimpleNamespace(fetchval=fetchval))
    return types.SimpleNamespace(app=app, redis=redis, cfg=cfg, played=played, panels=panels, feed=feed, asks=feed_asks)


def playing(q, item=A, position=10.0, duration=600.0, **kw):
    st = {"active": True, "item": item, "title": "Now playing", "kind": "video", "position": position,
          "duration": duration, **kw}
    run(q.redis.set(quasar.STATE.format(NODE["id"]), json.dumps(st)))
    return st


def results(q):
    return [e.data for e in q.panels if e.type == "panel.result"]


def test_add_next_remove_and_clear_keep_the_order_he_asked_for(q):
    playing(q)
    run(Q.remember_seen(q.redis, [{"id": B, "name": "Bee video", "minutes": 7, "channel": "Bees"}]))
    out = run(Q.tool(q.app, "owner", {"action": "add", "ids": f"{B}, {C}"}))
    assert out["headline"] == "Added 2 videos to your queue on TV PC" and q.played == []      # something's playing
    assert [(i["n"], i["title"]) for i in out["queue"]["items"]] == [(1, "Bee video"), (2, "A YouTube video")]
    assert out["queue"]["items"][0]["minutes"] == 7 and out["queue"]["items"][0]["channel"] == "Bees"
    out = run(Q.tool(q.app, "owner", {"action": "next", "ids": A}))
    assert [i["id"] for i in out["queue"]["items"]] == [A, B, C] and out["headline"].endswith("plays next on TV PC")
    out = run(Q.tool(q.app, "owner", {"action": "next", "what": "number 3"}))          # move one up
    assert [i["id"] for i in out["queue"]["items"]] == [C, A, B]
    out = run(Q.tool(q.app, "owner", {"action": "remove", "which": "bee"}))             # by its name
    assert [i["id"] for i in out["queue"]["items"]] == [C, A]
    out = run(Q.tool(q.app, "owner", {"action": "remove", "which": "1"}))
    assert [i["id"] for i in out["queue"]["items"]] == [A]
    with pytest.raises(ValueError, match="no “9”"):
        run(Q.tool(q.app, "owner", {"action": "remove", "which": "9"}))
    out = run(Q.tool(q.app, "owner", {"action": "clear"}))
    assert out["queue"]["items"] == [] and out["headline"] == "Cleared your queue on TV PC"
    assert out["queue"]["playing"]["title"] == "Now playing"


def test_adding_with_nothing_playing_starts_the_first_now(q):
    out = run(Q.tool(q.app, "owner", {"action": "add", "ids": f"{B} and {C}"}))
    assert q.played == [B] and [i["id"] for i in out["queue"]["items"]] == [C]
    assert out["headline"].endswith("· 1 queued after it")


def test_a_library_title_is_found_by_name(q, monkeypatch):
    JF_ID = JF[3:]

    class FakeJF:
        def __init__(self, redis): pass
        async def search(self, what, kind, limit): return [{"Id": JF_ID, "Type": "Movie", "Name": "Dune", "ProductionYear": 2021}]
        async def item(self, i): return {"Id": i, "Type": "Movie", "Name": "Dune", "ProductionYear": 2021, "RunTimeTicks": 155 * 60 * quasar.TICKS}
    monkeypatch.setattr(quasar, "Jellyfin", FakeJF)
    playing(q)
    out = run(Q.tool(q.app, "owner", {"action": "add", "what": "dune"}))
    it = out["queue"]["items"][0]
    assert it == {"n": 1, "id": JF, "kind": "library", "title": "Dune (2021)", "art": f"/quasar/art/{JF_ID}", "minutes": 155}


def test_near_the_end_he_gets_suggestions_once_and_the_top_one_is_kept_for_autoplay(q):
    st = playing(q, position=100)
    run(Q.on_state(q.app, NODE, st))
    assert results(q) == []                                         # not near the end yet
    st = playing(q, position=500)                                    # 100 s left of 600
    run(Q.on_state(q.app, NODE, st))
    run(Q.on_state(q.app, NODE, playing(q, position=510)))          # said once per video
    [d] = results(q)
    u = d["upnext"]
    assert d["headline"] == "1:40 left on TV PC · pick what's next" and q.asks == [{"feed": "home", "on": "TV PC", "limit": 24}]
    assert [i["id"] for i in u["catalog"]["items"]] == ["yt:eeeeeeeeeee", "yt:fffffffffff"]   # not what's playing, not live
    assert u["autoplay"] == "Top pick" and u["node"] == "TV PC" and u["ends_at"] > time.time()
    assert q.panels[0].tool == "quasar.next" and q.panels[0].device is None


def test_a_short_video_gets_them_halfway_through(q):
    run(Q.on_state(q.app, NODE, playing(q, position=80, duration=180)))     # 1:40 left: not yet
    run(Q.on_state(q.app, NODE, playing(q, item=B, position=95, duration=180)))
    assert [d["headline"] for d in results(q)] == ["1:25 left on TV PC · pick what's next"]


def test_with_something_lined_up_he_hears_what_and_no_suggestions_are_fetched(q):
    run(Q.on_state(q.app, NODE, playing(q, position=550, next="Frieren · S1E5")))
    assert results(q)[-1]["upnext"]["up_next"] == "Frieren · S1E5" and q.asks == []
    run(Q.save(q.redis, NODE["id"], [{"id": B, "kind": "youtube", "title": "Bee video"}]))
    run(Q.on_state(q.app, NODE, playing(q, item=B, position=550)))
    assert results(q)[-1]["headline"] == "Up next on TV PC: Bee video" and q.asks == []


def ended(q, item=A, position=595.0, duration=600.0):
    return {"active": False, "last": {"item": item, "title": "Now playing", "kind": "video", "position": position,
                                      "duration": duration}}


def test_when_a_video_ends_the_queue_goes_on_in_order(q):
    run(Q.save(q.redis, NODE["id"], [{"id": B, "kind": "youtube", "title": "Bee video"},
                                     {"id": C, "kind": "youtube", "title": "Sea video"}]))
    run(Q.on_state(q.app, NODE, ended(q)))
    assert q.played == [B] and [e["id"] for e in run(Q.entries(q.redis, NODE["id"]))] == [C]
    assert results(q)[-1]["upnext"]["started"] == "Bee video"
    closes = [e for e in q.panels if e.type == "panel.close"]
    assert closes and closes[-1].data["close_after"] == 20


def test_stopping_it_or_not_reaching_the_end_plays_nothing(q):
    run(Q.save(q.redis, NODE["id"], [{"id": B, "kind": "youtube", "title": "Bee video"}]))
    run(Q.stopped(q.redis, NODE["id"]))
    run(Q.on_state(q.app, NODE, ended(q)))                           # he said "stop"
    run(Q.on_state(q.app, NODE, ended(q, position=200)))             # it stopped partway (not the end)
    run(Q.on_state(q.app, NODE, {"active": False, "last": {**ended(q)["last"], "kind": "audio"}}))   # music
    assert q.played == [] and len(run(Q.entries(q.redis, NODE["id"]))) == 1
    assert not run(q.redis.get(Q.STOPPED.format(NODE["id"])))      # the stop counted once


def test_with_nothing_queued_autoplay_plays_the_top_suggestion_up_to_the_limit(q):
    q.cfg.autoplay_limit = 2
    run(Q.on_state(q.app, NODE, ended(q)))
    assert q.played == ["yt:eeeeeeeeeee"] and results(q)[-1]["headline"] == "Autoplay on TV PC: Top pick"
    run(Q.on_state(q.app, NODE, ended(q, item="yt:eeeeeeeeeee")))
    assert q.played == ["yt:eeeeeeeeeee", "yt:fffffffffff"]
    q.feed["items"].append({"id": "yt:hhhhhhhhhhh", "name": "Third", "type": "video"})
    run(Q.on_state(q.app, NODE, ended(q, item="yt:fffffffffff")))  # the third in a row with no word from him
    assert len(q.played) == 2 and "Autoplay stopped on TV PC after 2 in a row" in results(q)[-1]["headline"]
    run(Q.tool(q.app, "owner", {"action": "add", "ids": B}))         # he chose something: it counts afresh
    assert run(q.redis.get(Q.AUTO.format(NODE["id"]))) is None


def test_autoplay_off_by_his_word_or_his_settings(q):
    out = run(Q.tool(q.app, "owner", {"action": "autoplay_off"}))
    assert out["headline"].startswith("Autoplay is off")
    run(Q.on_state(q.app, NODE, ended(q)))
    assert q.played == []
    assert "turn on autoplay" in run(Q.view(q.app, NODE))["autoplay_note"]
    run(Q.tool(q.app, "owner", {"action": "autoplay_on"}))
    q.cfg.autoplay = False
    out = run(Q.tool(q.app, "owner", {"action": "autoplay_on"}))
    assert "settings" in out["headline"]
    run(Q.on_state(q.app, NODE, ended(q)))
    assert q.played == []


def test_a_queued_video_that_wont_play_is_said_and_the_next_one_plays(q, monkeypatch):
    async def picky(app_, user, node, vid, title=""):
        if vid == "bbbbbbbbbbb":
            raise ValueError("the stream may be unavailable")
        q.played.append(f"yt:{vid}")
        return {"headline": "Playing", "now": {"active": True}}
    monkeypatch.setattr(quasar, "play_youtube", picky)
    run(Q.save(q.redis, NODE["id"], [{"id": B, "kind": "youtube", "title": "Bee video"},
                                     {"id": C, "kind": "youtube", "title": "Sea video"}]))
    run(Q.on_state(q.app, NODE, ended(q)))
    assert q.played == [C]
    heads = [d["headline"] for d in results(q)]
    assert any("Couldn't play Bee video" in h for h in heads) and heads[-1].endswith("Sea video")


def test_a_library_entry_plays_just_that_one(q):
    run(Q.save(q.redis, NODE["id"], [{"id": JF, "kind": "library", "title": "Dune (2021)"}]))
    run(Q.on_state(q.app, NODE, ended(q)))
    assert q.played == [JF]


def test_skip_at_the_last_one_moves_to_the_queue_and_otherwise_leaves_it_to_the_player(q):
    playing(q, next="Frieren · S1E5")
    assert run(Q.skip(q.app, "owner", NODE)) is None                 # the player has its own next
    playing(q)
    run(Q.save(q.redis, NODE["id"], [{"id": B, "kind": "youtube", "title": "Bee video"}]))
    assert run(Q.skip(q.app, "owner", NODE)) is not None and q.played == [B]
    assert run(q.redis.get(Q.AUTO.format(NODE["id"]))) is None      # skipping isn't autoplay running on


def test_one_end_is_acted_on_once_and_one_move_at_a_time(q, monkeypatch):
    run(Q.save(q.redis, NODE["id"], [{"id": B, "kind": "youtube", "title": "Bee video"},
                                     {"id": C, "kind": "youtube", "title": "Sea video"}]))

    async def both(a, b):
        await asyncio.gather(Q.on_state(q.app, NODE, a), Q.on_state(q.app, NODE, b))
    run(both(ended(q), ended(q)))                                    # the same end, reported twice
    assert q.played == [B]
    slow = asyncio.Event()
    orig = quasar.play_youtube

    async def slow_play(app_, user, node, vid, title=""):
        await asyncio.sleep(0.2)
        return await orig(app_, user, node, vid, title)
    monkeypatch.setattr(quasar, "play_youtube", slow_play)
    run(Q.save(q.redis, NODE["id"], [{"id": C, "kind": "youtube", "title": "Sea video"},
                                     {"id": A, "kind": "youtube", "title": "Ay video"}]))
    run(both(ended(q, item=B), ended(q, item="yt:ggggggggggg")))   # two different ends at once: one move
    assert q.played == [B, C] and [e["id"] for e in run(Q.entries(q.redis, NODE["id"]))] == [A]
    assert run(q.redis.get(Q.BUSY.format(NODE["id"]))) is None       # and the lock is let go


def test_a_stop_while_the_next_one_is_on_its_way_is_kept(q, monkeypatch):
    from app import constellation
    stops = []

    async def player(app_, node, action, extra=None):
        stops.append(action)
        return {}
    monkeypatch.setattr(constellation, "_player", player)
    orig = quasar.play_youtube

    async def he_says_stop_meanwhile(app_, user, node, vid, title=""):
        await Q.stopped(q.redis, node["id"])                         # "stop", while it was starting
        return await orig(app_, user, node, vid, title)
    monkeypatch.setattr(quasar, "play_youtube", he_says_stop_meanwhile)
    run(Q.save(q.redis, NODE["id"], [{"id": B, "kind": "youtube", "title": "Bee video"}]))
    run(Q.on_state(q.app, NODE, ended(q)))
    assert stops == ["stop"] and "Stopped, as you asked" in results(q)[-1]["headline"]
    assert not run(q.redis.get(Q.STOPPED.format(NODE["id"])))        # used up: it won't stop a later video
    monkeypatch.setattr(quasar, "play_youtube", orig)
    run(Q.stopped(q.redis, NODE["id"]))                              # said before the next one started: nothing starts
    run(Q.save(q.redis, NODE["id"], [{"id": C, "kind": "youtube", "title": "Sea video"}]))
    assert run(Q.advance(q.app, "owner", NODE, last={"item": B})) is None
    assert q.played == [B] and [e["id"] for e in run(Q.entries(q.redis, NODE["id"]))] == [C]


def test_one_that_needs_something_first_waits_in_line_and_is_never_called_playing(q, monkeypatch):
    async def offer(app_, user, node, vid, title=""):
        return {"headline": "TV PC needs Node.js for YouTube", "confirm": {"tool": "constellation.install_package"}}
    monkeypatch.setattr(quasar, "play_youtube", offer)
    run(Q.save(q.redis, NODE["id"], [{"id": B, "kind": "youtube", "title": "Bee video"}]))
    out = run(Q.advance(q.app, "owner", NODE, last={"item": A}))
    assert out["confirm"]["tool"] == "constellation.install_package"
    assert [e["id"] for e in run(Q.entries(q.redis, NODE["id"]))] == [B]          # back in line
    assert "Couldn't play Bee video from your queue: TV PC needs Node.js" in results(q)[-1]["headline"]
    out = run(Q.tool(q.app, "owner", {"action": "add", "ids": C}))   # nothing playing: the first would start now
    assert out["confirm"] and [i["id"] for i in out["queue"]["items"]] == [C, B]


def test_autoplay_never_bounces_back_to_what_it_just_played(q):
    run(Q.on_state(q.app, NODE, ended(q)))                           # A ends: the top pick (E)
    run(Q.on_state(q.app, NODE, ended(q, item="yt:eeeeeeeeeee")))    # E ends: the feed's first is A, which just played
    assert q.played == ["yt:eeeeeeeeeee", "yt:fffffffffff"]
    run(Q.on_state(q.app, NODE, ended(q, item="yt:fffffffffff")))    # F ends: everything left just played
    assert q.played == ["yt:eeeeeeeeeee", "yt:fffffffffff"]


def test_his_own_play_or_skip_clears_an_old_stop(q):
    run(Q.stopped(q.redis, NODE["id"]))                              # "stop" with nothing playing: no report follows
    playing(q)
    run(Q.save(q.redis, NODE["id"], [{"id": B, "kind": "youtube", "title": "Bee video"}]))
    assert run(Q.skip(q.app, "owner", NODE)) is not None and q.played == [B]     # his skip isn't swallowed
    run(Q.stopped(q.redis, NODE["id"]))
    run(Q.tool(q.app, "owner", {"action": "add", "ids": C}))
    assert not run(q.redis.get(Q.STOPPED.format(NODE["id"])))


def test_going_home_counts_as_stop(q, monkeypatch):
    async def target(app_, asked, screen=False):
        return {"id": 5, "name": "TV PC"}

    async def ask_node(app_, node, kind, args, timeout=20):
        return {"stopped": "Now playing"}

    async def no_tv(app_, user, node):
        return {}
    monkeypatch.setattr(constellation, "target", target)
    monkeypatch.setattr(constellation, "_ask_node", ask_node)
    monkeypatch.setattr(constellation, "show_on_tv", no_tv)
    run(constellation.home(q.app, "owner", {}))
    assert run(q.redis.get(Q.STOPPED.format(5)))


def test_library_suggestions_leave_out_what_just_played(q, monkeypatch):
    cur = "0123456789abcdef0123456789abcdef"

    class JF:
        def __init__(self, redis): pass
        async def next_up(self, limit=12): return [{"Id": cur, "Type": "Episode", "Name": "Now", "SeriesName": "Frieren"},
                                                   {"Id": "n2", "Type": "Episode", "Name": "Next", "SeriesName": "Frieren",
                                                    "ParentIndexNumber": 1, "IndexNumber": 5, "SeriesId": "s1"}]
        async def items(self, **kw): return [{"Id": "m1", "Type": "Movie", "Name": "Dune"}] if "DateCreated" in str(kw) else []
    monkeypatch.setattr(quasar, "Jellyfin", JF)
    sug = run(Q._suggest(q.app, NODE, f"jf:{cur}"))
    assert [g["name"] for g in sug["catalog"]["groups"]] == ["Next up", "Recently added"]
    assert sug["top"]["id"] == "jf:n2" and sug["top"]["title"] == "Frieren · S1E5 · Next"


def test_stop_and_skip_through_constellation_reach_the_queue(q, monkeypatch):
    calls = []

    async def target(app_, asked, screen=False):
        return {"id": 5, "name": "TV PC", "shell": "off"}

    async def player(app_, node, action, extra=None):
        calls.append(action)
        return {"headline": "done"}
    monkeypatch.setattr(constellation, "target", target)
    monkeypatch.setattr(constellation, "_player", player)
    run(constellation.media(q.app, "owner", {"action": "stop"}))
    assert calls == ["stop"] and run(q.redis.get(Q.STOPPED.format(5)))
    run(Q.on_state(q.app, NODE, ended(q, position=200)))             # the player's last report uses it up
    assert not run(q.redis.get(Q.STOPPED.format(5)))
    playing(q)
    run(Q.save(q.redis, 5, [{"id": B, "kind": "youtube", "title": "Bee video"}]))
    out = run(constellation.media(q.app, "owner", {"action": "next"}))
    assert calls == ["stop"] and q.played == [B] and out["headline"].startswith("Playing")


def test_every_queue_id_is_one_of_ours():
    assert Q.IDS.findall("add yt:abcdefghijk, jf:0123456789abcdef0123456789abcdef and yt:short to my queue") == \
        ["yt:abcdefghijk", "jf:0123456789abcdef0123456789abcdef"]
    assert Q.IDS.findall("add yt:abcdefghij-, yt:-bcdefghijk to my queue") == ["yt:abcdefghij-", "yt:-bcdefghijk"]
    assert Q.IDS.fullmatch("yt:abcdefghij-") and not Q.IDS.findall("yt:abcdefghijkl")      # 12 long: not an id
