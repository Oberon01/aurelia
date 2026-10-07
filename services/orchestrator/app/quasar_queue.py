"""Quasar's queue (Phase 12.5): what plays after what's playing, on each of his computers.

The queue is mine, not the player's: YouTube and his library mix in it, and the player can't hold both in
one list (his library's streams carry my key, which must never go with a YouTube video). When the player
reports that a video ended (its own last report: inactive, at the end), the next one in line starts; with
nothing queued, the top suggestion does (autoplay), at most autoplay_limit in a row with no word from him.
Something counts as playing only when the player says so; one that needs something first (my player, Node.js)
goes back in line and he's told why.

Near the end of a video (suggest_seconds before, or halfway through a short one) I say what's next: what's
lined up (the player's own next episode, or the queue), or, with nothing lined up, suggestions to pick from
(his YouTube home feed for a YouTube video, his library for one of his). That panel goes to his displays;
his phone shows it as a bar he can tap.

He stopped it ("stop", or sending the computer home): nothing plays next, even when the next one was already
on its way. "Skip" at the last one moves to the queue.
"""
import asyncio
import json
import logging
import re
import secrets
import time
from typing import Any, Callable

from redis.exceptions import WatchError

from . import quasar

log = logging.getLogger("aurelia.quasar.queue")

KEY = "aurelia:queue:{}"                       # the queue on one computer: a JSON list of entries
NEAR = "aurelia:queue:{}:near:{}"              # what's next was already said for this item
STOPPED = "aurelia:queue:{}:stopped"           # he stopped it: nothing plays next
AUTO = "aurelia:queue:{}:auto"                 # autoplayed in a row, with no word from him
SUGGEST = "aurelia:queue:{}:suggest"           # suggestions for what's playing (the top one autoplays)
RECENT = "aurelia:queue:{}:recent"             # what autoplay played lately (never again soon: no A, B, A)
BUSY = "aurelia:queue:{}:busy"                 # one move to the next at a time
ENDED = "aurelia:queue:{}:ended:{}"            # this video's end was already acted on
OFF = "aurelia:autoplay:off"                   # "turn off autoplay" (until he turns it back on)
SEEN = "aurelia:quasar:seen"                   # YouTube titles I've shown him, by id (a queued id says its name)
MAX = 100
ENDED_WITHIN = 20.0                            # the last report this close to the end: it ended (not stopped)
SHORT = 240.0                                  # under this long: suggestions halfway through
BUSY_SECONDS = 200                             # longer than the slowest play (YouTube: 150 s)
IDS = re.compile(r"(?<![\w-])(yt:[A-Za-z0-9_-]{11}|jf:[0-9A-Za-z]{1,64})(?![\w-])")
_tasks: set = set()                            # background work, kept until it's done


# ---- the queue itself ----------------------------------------------------------------------------------

def _parse(raw) -> list[dict]:
    try:
        got = json.loads(raw) if raw else []
    except ValueError:
        got = []
    return [e for e in got if isinstance(e, dict) and IDS.fullmatch(str(e.get("id") or ""))]


async def entries(redis, node_id: int) -> list[dict]:
    return _parse(await redis.get(KEY.format(node_id)))


async def mutate(redis, node_id: int, change: Callable[[list[dict]], tuple[list[dict], Any]]) -> Any:
    """Change the queue in one step (another change meanwhile: read it again and redo), so an addition and
    the next one starting never undo each other."""
    key = KEY.format(node_id)
    async with redis.pipeline(transaction=True) as pipe:
        while True:
            try:
                await pipe.watch(key)
                items, out = change(_parse(await pipe.get(key)))
                pipe.multi()
                if items:
                    pipe.set(key, json.dumps(items[:MAX]))
                else:
                    pipe.delete(key)
                await pipe.execute()
                return out
            except WatchError:
                continue


async def save(redis, node_id: int, items: list[dict]) -> None:
    await mutate(redis, node_id, lambda _: (list(items), None))


async def remember_seen(redis, items: list[dict]) -> None:
    """YouTube videos shown on a panel: a queued id can say its name later."""
    m = {i["id"]: json.dumps({k: i[k] for k in ("name", "art", "minutes", "channel") if i.get(k)})
         for i in items if str(i.get("id") or "").startswith("yt:")}
    if m:
        await redis.hset(SEEN, mapping=m)
        await redis.expire(SEEN, 3 * 86400)


async def _entry(app, ident: str, jf=None) -> dict:
    """One queue entry for an id: its title and picture (a library item read from Jellyfin)."""
    if ident.startswith("yt:"):
        raw = await app.state.redis.hget(SEEN, ident)
        seen = json.loads(raw) if raw else {}
        return {"id": ident, "kind": "youtube", "title": seen.get("name") or "A YouTube video",
                "art": f"/quasar/ytart/{ident[3:]}", **({"minutes": seen["minutes"]} if seen.get("minutes") else {}),
                **({"channel": seen["channel"]} if seen.get("channel") else {})}
    jf = jf or quasar.Jellyfin(app.state.redis)
    it = await jf.item(ident[3:])
    s = quasar.shape(it)
    out = {"id": f"jf:{it['Id']}", "kind": "library", "title": quasar.label(it), "art": s["art"]}
    if s.get("minutes"):
        out["minutes"] = s["minutes"]
    return out


def _state(raw) -> dict:
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


async def player_of(redis, node_id: int) -> dict:
    """What my player there last said (kept 30 s)."""
    return _state(await redis.get(quasar.STATE.format(node_id)))


async def _node(app, on: str) -> dict:
    """The computer he means; with no name, the one my player is playing video on (else his usual one)."""
    on = " ".join(str(on or "").split())
    if on:
        return await quasar._where(app, on)
    redis = app.state.redis
    playing = []
    async for k in redis.scan_iter(match=quasar.STATE.format("*")):
        key = k.decode() if isinstance(k, bytes) else k
        if key.count(":") != 2:
            continue
        st = _state(await redis.get(key))
        if st.get("active"):
            playing.append(((st.get("kind") or "video") == "video", st.get("at") or 0, int(key.rsplit(":", 1)[1])))
    if playing:
        from . import db
        nid = max(playing)[2]                          # video first, then the latest
        name = await db.pool().fetchval("SELECT name FROM devices WHERE id=$1", nid)
        if name:
            return await quasar._where(app, name)
    return await quasar._where(app, "")


def autoplay_on(app) -> bool:
    return bool(app.state.config.current.quasar.autoplay)


async def view(app, node: dict) -> dict:
    """The queue panel: what's playing, what's lined up, and what autoplay would play."""
    redis = app.state.redis
    st = await player_of(redis, node["id"])
    items = await entries(redis, node["id"])
    on = autoplay_on(app) and not await redis.get(OFF)
    out: dict[str, Any] = {"node": node["name"], "items": [{"n": i + 1, **e} for i, e in enumerate(items)],
                           "autoplay": on}
    if st.get("active"):
        out["playing"] = quasar.now_block(node["name"], st)
        sug = _state(await redis.get(SUGGEST.format(node["id"])))
        if on and not items and not st.get("next") and sug.get("item") == st.get("item") and sug.get("top"):
            out["autoplay_next"] = sug["top"]["title"]
    if not on:
        out["autoplay_note"] = ("Autoplay is off (\"turn on autoplay\")." if autoplay_on(app)
                                else "Autoplay is off in your settings (quasar.autoplay).")
    return out


# ---- the tool -----------------------------------------------------------------------------------------

async def tool(app, user: str, args: dict) -> dict:
    action = str(args.get("action") or "show").lower().replace(" ", "_")
    redis = app.state.redis
    if action in ("autoplay_on", "autoplay_off"):
        if action == "autoplay_off":
            await redis.set(OFF, "1")
        else:
            await redis.delete(OFF)
            if not autoplay_on(app):
                return {"headline": "Autoplay is off in your settings", "note": "Set quasar.autoplay: true in "
                        "config/aurelia.yaml to turn it on."}
        return {"headline": "Autoplay is off: when the queue runs out, the video just ends" if action == "autoplay_off"
                else "Autoplay is on: when the queue runs out, the top suggestion plays"}
    node = await _node(app, str(args.get("on") or ""))
    nid = node["id"]
    if action == "show":
        n = len(await entries(redis, nid))
        head = f"Your queue on {node['name']} · {n} video{'s' if n != 1 else ''}" if n else f"Nothing queued on {node['name']}"
        return {"headline": head, "queue": await view(app, node)}
    if action == "clear":
        had = await mutate(redis, nid, lambda items: ([], len(items)))
        return {"headline": f"Cleared your queue on {node['name']}" if had else f"Your queue on {node['name']} was empty",
                "queue": await view(app, node)}
    if action == "remove":
        which = " ".join(str(args.get("which") or "").split())

        def drop(items):
            idx = _pick(items, which)
            return (items, None) if idx is None else (items[:idx] + items[idx + 1:], items[idx])
        gone = await mutate(redis, nid, drop)
        if gone is None:
            raise ValueError(f"There's no “{which}” in your queue on {node['name']}." if which else "Remove which one?")
        return {"headline": f"Removed {gone['title']} from your queue"[:140], "queue": await view(app, node)}
    if action not in ("add", "next"):
        raise ValueError("The queue can show, add, next, remove, clear, autoplay_on or autoplay_off.")
    what = " ".join(str(args.get("what") or "").split())
    ids = IDS.findall(str(args.get("ids") or "")) + IDS.findall(what)
    if action == "next" and not ids and what:              # "play number 3 next": move it up
        which = re.sub(r"^(?:number|#|no\.?)\s*", "", what, flags=re.I)

        def up(items):
            idx = _pick(items, which)
            return (items, None) if idx is None else ([items[idx]] + items[:idx] + items[idx + 1:], items[idx])
        moved = await mutate(redis, nid, up)
        if moved is not None:
            await redis.delete(AUTO.format(nid))
            return {"headline": f"{moved['title']} plays next on {node['name']}"[:140], "queue": await view(app, node)}
    new: list[dict] = []
    jf = None
    if ids:
        for ident in dict.fromkeys(ids):                    # in order, once each
            if ident.startswith("jf:") and jf is None:
                jf = quasar.Jellyfin(redis)
            new.append(await _entry(app, ident, jf))
    elif what:                                              # a title from his library
        jf = quasar.Jellyfin(redis)
        it = quasar.best(await jf.search(what, "any", 25), what)
        if it is None:
            raise ValueError(f"I couldn't find “{what}” in your library.")
        new.append(await _entry(app, f"jf:{it['Id']}", jf))
    if not new:
        raise ValueError("Add what to the queue?")
    await redis.delete(AUTO.format(nid), STOPPED.format(nid))   # he chose: autoplay counts afresh; an old stop is done
    st = await player_of(redis, nid)
    if not st.get("active"):                                # nothing playing there: the first one plays now
        first, rest = new[0], new[1:]
        await mutate(redis, nid, lambda items: ((rest + items) if action == "next" else (items + rest), None))
        played = await play_entry(app, user, node, first)
        if not _playing(played):                            # it needs something first: it waits, first in line
            await mutate(redis, nid, lambda items: ([first] + items, None))
            return {**played, "queue": await view(app, node)}
        more = f" · {len(rest)} queued after it" if rest else ""
        return {**played, "headline": f"{played.get('headline') or 'Playing'}{more}"[:160], "queue": await view(app, node)}
    await mutate(redis, nid, lambda items: ((new + items) if action == "next" else (items + new), None))
    names = new[0]["title"] if len(new) == 1 else f"{len(new)} videos"
    head = f"{names} plays next on {node['name']}" if action == "next" else f"Added {names} to your queue on {node['name']}"
    return {"headline": head[:160], "queue": await view(app, node)}


def _pick(items: list[dict], which: str) -> int | None:
    if not which:
        return None
    if which.isdigit():
        i = int(which) - 1
        return i if 0 <= i < len(items) else None
    ids = IDS.findall(which)
    if ids:
        return next((i for i, e in enumerate(items) if e["id"] == ids[0]), None)
    w = quasar._norm(which)
    return next((i for i, e in enumerate(items) if w and w in quasar._norm(e.get("title") or "")), None)


# ---- playing what's next -----------------------------------------------------------------------------

def _playing(out: Any) -> bool:
    """Playing, as the player said it (not an offer to install something first)."""
    return isinstance(out, dict) and not out.get("confirm") and bool((out.get("now") or {}).get("active"))


async def play_entry(app, user: str, node: dict, e: dict) -> dict:
    if e["id"].startswith("yt:"):
        return await quasar.play_youtube(app, user, node, e["id"][3:], e.get("title") if e.get("title") != "A YouTube video" else "")
    return await quasar.play(app, user, {"id": e["id"][3:], "on": node["name"], "_one": True, "_auto": True})


async def _try(app, user: str, node: dict, e: dict) -> tuple[dict | None, str | None]:
    """(what the player said, or None), and why it didn't play."""
    try:
        out = await play_entry(app, user, node, e)
    except ValueError as exc:
        return None, str(exc)
    except Exception as exc:                               # never lost silently: he hears it
        log.exception("playing %s on %s", e.get("id"), node.get("name"))
        return None, f"something went wrong ({exc.__class__.__name__})"
    if not _playing(out):
        return out, (out or {}).get("headline") or "it didn't start"
    return out, None


async def _stop_now(app, node: dict) -> None:
    """He said stop while the next one was on its way: it stops as soon as it starts."""
    from . import constellation
    try:
        await constellation._player(app, node, "stop")
    except Exception:
        log.exception("stopping %s after he said stop", node.get("name"))


async def advance(app, user: str, node: dict, why: str = "ended", last: dict | None = None) -> dict | None:
    """The next in line plays (or, with nothing queued, the top suggestion). None when nothing does."""
    redis = app.state.redis
    nid = node["id"]
    token = secrets.token_hex(8)
    if not await redis.set(BUSY.format(nid), token, nx=True, ex=BUSY_SECONDS):
        return None

    async def halted() -> bool:                           # he said stop meanwhile (the flag is his: used up here)
        return bool(await redis.getdel(STOPPED.format(nid)))
    try:
        for _ in range(3):                                 # one that won't play doesn't stop the rest
            if await halted():
                return None
            e = await mutate(redis, nid, lambda items: (items[1:], items[0] if items else None))
            if e is None:
                break
            out, problem = await _try(app, user, node, e)
            if problem is None:
                if await halted():
                    await _stop_now(app, node)
                    await _say(app, node, f"Stopped, as you asked (the next one had already started).", last)
                    return None
                await _say(app, node, f"Next from your queue on {node['name']}: {e['title']}"[:200], last, started=e)
                return out
            if out is not None:                            # it needs something first: back in line, and he's told
                await mutate(redis, nid, lambda items: ([e] + items, None))
                await _say(app, node, f"Couldn't play {e['title']} from your queue: {problem}"[:200], last)
                return out
            await _say(app, node, f"Couldn't play {e['title']} from your queue: {problem}"[:200], last)
        if await entries(redis, nid):                      # three in a row wouldn't play: the rest wait for him
            return None
        if not autoplay_on(app) or await redis.get(OFF):
            return None
        item = (last or {}).get("item")
        if not item:
            return None
        await redis.lpush(RECENT.format(nid), item)        # what just played isn't autoplayed back (no A, B, A)
        await redis.ltrim(RECENT.format(nid), 0, 19)
        await redis.expire(RECENT.format(nid), 6 * 3600)
        sug = _state(await redis.get(SUGGEST.format(nid)))
        if not sug.get("top") or sug.get("item") != item:
            try:
                sug = await _suggest(app, node, item)
            except Exception as exc:
                log.info("no suggestions for autoplay on %s: %s", node["name"], exc)
                return None
        top = sug.get("top")
        if not top:
            return None
        n = int(await redis.get(AUTO.format(nid)) or 0)
        limit = app.state.config.current.quasar.autoplay_limit
        if why == "ended" and n >= limit:
            await _say(app, node, f"Autoplay stopped on {node['name']} after {n} in a row. Say “play …” or pick "
                                  "something to keep going.", last)
            return None
        if await halted():
            return None
        await redis.delete(SUGGEST.format(nid))
        out, problem = await _try(app, user, node, top)
        if problem is not None:
            await _say(app, node, f"Autoplay couldn't play {top['title']}: {problem}"[:200], last)
            return out
        if await halted():
            await _stop_now(app, node)
            await _say(app, node, "Stopped, as you asked (autoplay had already started the next one).", last)
            return None
        if why == "ended":
            await redis.set(AUTO.format(nid), n + 1, ex=12 * 3600)
        await redis.lpush(RECENT.format(nid), top["id"])
        await redis.ltrim(RECENT.format(nid), 0, 19)
        await redis.expire(RECENT.format(nid), 6 * 3600)
        await _say(app, node, f"Autoplay on {node['name']}: {top['title']}"[:200], last, started=top)
        return out
    finally:
        if await redis.get(BUSY.format(nid)) in (token, token.encode()):
            await redis.delete(BUSY.format(nid))


async def skip(app, user: str, node: dict) -> dict | None:
    """'Skip' at the last one in the player: the queue (or the top suggestion). None: nothing to move to."""
    st = await player_of(app.state.redis, node["id"])
    if not st.get("active") or st.get("next"):
        return None
    await app.state.redis.delete(STOPPED.format(node["id"]))   # "skip" is his word now: an older stop is done
    return await advance(app, user, node, why="skip", last={k: st.get(k) for k in ("title", "item", "kind")})


async def stopped(redis, node_id: int) -> None:
    """He stopped it: what ends now (or what was about to start) isn't followed by anything."""
    await redis.set(STOPPED.format(node_id), "1", ex=120)


# ---- what the player reports -------------------------------------------------------------------------

def _near_at(duration: float, lead: float) -> float:
    return duration / 2 if duration < SHORT else lead


async def on_state(app, dev: dict, st: dict) -> None:
    """Every player report: near the end, say what's next; at the end, play it."""
    redis = app.state.redis
    try:
        if st.get("active"):
            dur, pos, item = st.get("duration"), st.get("position"), str(st.get("item") or "")
            if (st.get("kind") or "video") != "video" or not dur or pos is None or not item:
                return
            left = float(dur) - float(pos)
            if left > _near_at(float(dur), float(app.state.config.current.quasar.suggest_seconds)) or left < 5:
                return
            if not await redis.set(NEAR.format(dev["id"], item), "1", nx=True, ex=int(max(600, float(dur)))):
                return
            node = await quasar._where(app, dev["name"])
            await near_end(app, node, st)
            return
        last = st.get("last") or {}
        if not last:
            return
        dur, pos = last.get("duration"), last.get("position")
        ended = (last.get("kind") or "video") == "video" and dur and pos is not None and float(dur) - float(pos) <= ENDED_WITHIN
        if not ended:                                       # stopped partway (by him, or it failed): nothing follows,
            await redis.delete(STOPPED.format(dev["id"]))   # and a stop of his is used up here
            return
        if not await redis.set(ENDED.format(dev["id"], last.get("item") or ""), "1", nx=True, ex=60):
            return                                          # (one end, however many times it's reported)
        if await redis.getdel(STOPPED.format(dev["id"])):
            return
        node = await quasar._where(app, dev["name"])
        await advance(app, "owner", node, why="ended", last=last)
    except Exception:
        log.exception("queue on %s", dev.get("name"))


async def _suggest(app, node: dict, item: str) -> dict:
    """What to pick from next: his YouTube home feed for a YouTube video, his library for one of his.
    The top one is what autoplay plays (never what's playing, nor what autoplay played lately)."""
    redis = app.state.redis
    recent = {(x.decode() if isinstance(x, bytes) else x) for x in await redis.lrange(RECENT.format(node["id"]), 0, -1)}
    if item.startswith("yt:"):
        d = await quasar.youtube(app, "owner", {"feed": "home", "on": node["name"], "limit": 24})
        cat = d.get("catalog") or {}
        got = [x for x in cat.get("items") or [] if x.get("id") != item and not x.get("live")][:20]
        top = next(({"id": x["id"], "kind": "youtube", "title": x.get("name") or "A YouTube video",
                     "art": x.get("art")} for x in got if x["id"] not in recent), None)
        catalog = {"source": "youtube", "node": node["name"], "items": got}
        note = d.get("note")
    else:
        jf = quasar.Jellyfin(redis)
        cur = item[3:] if item.startswith("jf:") else ""
        up = [i for i in await jf.next_up(limit=10) if i.get("Id") != cur]
        resume = [i for i in await jf.items(filters="IsResumable", sortBy="DatePlayed", sortOrder="Descending",
                                            includeItemTypes="Movie,Episode", limit=10) if i.get("Id") != cur]
        recent_added = [i for i in await jf.items(sortBy="DateCreated", sortOrder="Descending",
                                                  includeItemTypes="Movie,Series", limit=10) if i.get("Id") != cur]
        seen: set = set()
        groups = []
        for name, its in (("Next up", up), ("Continue", resume), ("Recently added", recent_added)):
            keep = [i for i in its if i.get("Id") not in seen][:8]
            seen.update(i.get("Id") for i in keep)
            if keep:
                groups.append({"name": name, "items": [quasar.shape(i) for i in keep]})
        first = next((i for g in (up, resume, recent_added) for i in g if f"jf:{i.get('Id')}" not in recent), None)
        top = None
        if first is not None:
            top = {"id": f"jf:{first['Id']}", "kind": "library", "title": quasar.label(first),
                   "art": quasar.shape(first)["art"]}
        catalog = {"source": "library", "node": node["name"], "groups": groups}
        note = None
    out = {"item": item, "top": top, "catalog": catalog, **({"note": note} if note else {})}
    await redis.set(SUGGEST.format(node["id"]), json.dumps({"item": item, "top": top}), ex=6 * 3600)
    return out


def _left(secs: float) -> str:
    s = max(0, int(round(secs)))
    return f"{s // 60}:{s % 60:02d}" if s >= 60 else f"{s} s"


async def near_end(app, node: dict, st: dict) -> None:
    redis = app.state.redis
    left = max(0.0, float(st["duration"]) - float(st["position"]))
    items = await entries(redis, node["id"])
    up = st.get("next") or (items[0]["title"] if items else None)
    base = {"node": node["name"], "node_id": node["id"], "playing": st.get("title") or "",
            "item": st.get("item"), "ends_at": round(time.time() + left, 1)}
    if up:
        await _publish(app, node, {"headline": f"Up next on {node['name']}: {up}"[:160],
                                   "upnext": {**base, "up_next": up, "queued": len(items)}})
        return
    try:
        sug = await _suggest(app, node, str(st.get("item") or ""))
    except Exception as exc:
        log.info("no suggestions on %s: %s", node["name"], exc)
        await _publish(app, node, {"headline": f"{_left(left)} left on {node['name']} · nothing queued",
                                   "upnext": {**base, "problem": f"I couldn't get suggestions ({exc})."[:200]}})
        return
    on = autoplay_on(app) and not await redis.get(OFF)
    data = {"headline": f"{_left(left)} left on {node['name']} · pick what's next",
            "upnext": {**base, "catalog": sug["catalog"], "source": sug["catalog"]["source"],
                       **({"autoplay": sug["top"]["title"]} if on and sug.get("top") else {}),
                       **({"note": sug["note"]} if sug.get("note") else {})}}
    await _publish(app, node, data)


def _task(node: dict) -> str:
    return f"upnext-{node['id']}"


async def _publish(app, node: dict, data: dict, close_after: int | None = None) -> None:
    """The what's-next panel on his displays (his phone shows it as a bar). One per computer."""
    from .events import PanelEvent, publish
    redis = app.state.redis
    tid = _task(node)
    await publish(redis, PanelEvent(type="panel.open", task_id=tid, tool="quasar.next",
                                    data={"tier": "reference", "args": {}, "key": f"upnext:{node['id']}",
                                          "cancellable": False}))
    await publish(redis, PanelEvent(type="panel.result", task_id=tid, tool="quasar.next",
                                    data={"status": "completed", "partial": False, **data}))
    if close_after is not None:
        await publish(redis, PanelEvent(type="panel.close", task_id=tid, tool="quasar.next",
                                        data={"status": "completed", "has_result": True, "close_after": close_after}))


async def _say(app, node: dict, text: str, last: dict | None, started: dict | None = None) -> None:
    data: dict[str, Any] = {"headline": text, "upnext": {"node": node["name"], "node_id": node["id"],
                                                         "playing": (last or {}).get("title") or "", "said": text}}
    if started:
        data["upnext"]["started"] = started.get("title")
    await _publish(app, node, data, close_after=20)


def schedule(app, dev: dict, st: dict) -> None:
    """From the player's report endpoint: never holds it up (and the work is kept until it's done)."""
    t = asyncio.create_task(on_state(app, dev, st))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
