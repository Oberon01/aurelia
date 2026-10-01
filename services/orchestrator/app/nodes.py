"""The node connection (Phase 7 step 2): how a computer running Aurelia's node agent
talks to her.

A node never opens a port. It calls out, over the tailnet, with its own key:
  POST /nodes/hello                  what it is (OS, version, screens, hardware) and how it's doing (vitals)
  GET  /nodes/commands?wait=25       waits for work (long poll); this is also its heartbeat
  POST /nodes/commands/{id}/result   how a command went
  POST /nodes/screen                 a one-time sign-in link for its own shell (kiosk browser)
  POST /nodes/update-failed          its agent couldn't update after max_attempts tries
Each hello answers with the agent version Aurelia serves: nodes update themselves
(Kentrell's standing decision, in Constellation's policy: updates.automatic).
Commands are queued per node in Redis. A node is online while it keeps polling
(offline_after_seconds, from Constellation's policy).

Keys are checked here as well as at the proxy: only an approved *node* key reaches
these endpoints, and each node only ever sees its own queue.

Serving the agent itself (/node/agent.py, /node/install-linux.sh) needs no key: it's
code, no secrets, and a machine can't have a key before it pairs.
"""
import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from . import auth, db

log = logging.getLogger("aurelia.nodes")
router = APIRouter(tags=["nodes"])

QUEUE = "aurelia:node:{}:commands"
ONLINE = "aurelia:node:{}:online"
RESULT = "aurelia:nodecmd:{}:result"
RETRY = "aurelia:node:{}:retry_update"
COMMAND_TTL = 120                     # a command nobody picked up in 2 minutes is dropped, never run late
DEFAULT_OFFLINE_AFTER = 45
AGENT_DIR = Path(__file__).resolve().parent / "node_agent"


def policy_rules(app) -> dict:
    policy = app.state.governance.functions.get("constellation")
    return (policy.rules if policy else None) or {}


def update_rules(app) -> dict:
    u = policy_rules(app).get("updates") or {}
    return {"automatic": bool(u.get("automatic", True)), "max_attempts": int(u.get("max_attempts", 3))}


_release: tuple[float, dict] = (0.0, {})


def agent_release() -> dict:
    """The agent Aurelia serves right now: its version and checksum (cached until the file changes)."""
    global _release
    path = AGENT_DIR / "aurelia_node.py"
    mtime = path.stat().st_mtime
    if _release[0] != mtime:
        data = path.read_bytes()
        m = re.search(rb'^VERSION = "([^"]+)"', data, re.M)
        _release = (mtime, {"version": m.group(1).decode() if m else "?", "sha256": hashlib.sha256(data).hexdigest()})
    return _release[1]


def offline_after(app) -> int:
    policy = app.state.governance.functions.get("constellation")
    comm = ((policy.rules if policy else None) or {}).get("communication") or {}
    return int(comm.get("offline_after_seconds", DEFAULT_OFFLINE_AFTER))


async def node_from(request: Request) -> dict[str, Any]:
    dev = await auth.device_for(auth.key_from(request))
    if not dev or dev["kind"] != "node":
        raise HTTPException(401, "Only a paired node reaches this.")
    return dev


# ---- the node's side ----------------------------------------------------------------------

class Hello(BaseModel):
    info: dict[str, Any] = {}
    specs: dict[str, Any] = {}          # its hardware (agent 0.5+)
    vitals: dict[str, Any] = {}         # a small health reading with every check-in (agent 0.5+)


# ---- node health: what a check-in reports, and when it's worth telling him --------------------

HEALTH_DEFAULTS = {"disk_warn_percent": 90, "disk_urgent_percent": 97, "memory_warn_percent": 90,
                   "temp_warn_c": 85, "temp_urgent_c": 95, "cpu_busy_percent": 90, "cpu_busy_checkins": 3,
                   "battery_low_percent": 10}
RECENT = "aurelia:node:{}:vitals"


def health_rules(app) -> dict:
    r = dict(HEALTH_DEFAULTS)
    for k, v in (policy_rules(app).get("health") or {}).items():
        if k in r and isinstance(v, (int, float)):
            r[k] = v
    return r


def clean_tree(v: Any, depth: int = 0) -> Any:
    """Whatever a node reports, bounded: nested dicts and lists of plain values only."""
    if depth > 4:
        return None
    if isinstance(v, dict):
        return {str(k)[:40]: clean_tree(x, depth + 1) for k, x in list(v.items())[:30]}
    if isinstance(v, list):
        return [clean_tree(x, depth + 1) for x in v[:20]]
    if isinstance(v, str):
        return v[:120]
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    return None


def issues(v: dict, recent_cpu: list[float], r: dict) -> list[dict]:
    """What's wrong with a computer, in plain words: [{key, level (warn|urgent), text}]."""
    out = []
    for d in v.get("disks") or []:
        used = d.get("used") or 0
        if used >= r["disk_warn_percent"]:
            out.append({"key": f"disk:{d.get('mount')}", "level": "urgent" if used >= r["disk_urgent_percent"] else "warn",
                        "text": f"Disk {d.get('mount')} is {used}% full ({d.get('free_gb')} GB free)"})
    if (v.get("memory") or 0) >= r["memory_warn_percent"]:
        out.append({"key": "memory", "level": "warn", "text": f"Memory is {v['memory']}% used"
                    + (f" and it's swapping ({v['swap']}% of swap)" if (v.get("swap") or 0) >= 25 else "")})
    hot = max([t for t in (v.get("temp"), v.get("gpu_temp")) if isinstance(t, (int, float))], default=None)
    if hot is not None and hot >= r["temp_warn_c"]:
        out.append({"key": "temp", "level": "urgent" if hot >= r["temp_urgent_c"] else "warn",
                    "text": f"It's running hot ({hot:.0f}°C)"})
    n = int(r["cpu_busy_checkins"])
    if len(recent_cpu) >= n and all(c >= r["cpu_busy_percent"] for c in recent_cpu[:n]):
        out.append({"key": "cpu", "level": "warn", "text": f"The CPU has been over {r['cpu_busy_percent']}% busy "
                                                          f"for its last {n} check-ins"})
    b = v.get("battery") or {}
    if isinstance(b, dict) and b.get("percent") is not None and not b.get("charging") and b["percent"] <= r["battery_low_percent"]:
        out.append({"key": "battery", "level": "warn", "text": f"Battery is at {b['percent']}% and not charging"})
    return out


async def _alert(redis, dev: dict, urgent: list[dict], advice: str | None = None) -> None:
    """An urgent problem gets one mailbox card per problem per day (the digest mentions the rest)."""
    from . import mailbox
    from datetime import date
    made = False
    for i in urgent:
        item = await db.pool().fetchval(
            "INSERT INTO mailbox_items (kind, fingerprint, source, title, summary, proposal, evidence, strength, confidence, "
            "effect, urgent) SELECT 'node_alert', $1, 'constellation', $2, $3, $4::jsonb, '[]'::jsonb, 1, 1, $5, true "
            "WHERE NOT EXISTS (SELECT 1 FROM mailbox_items WHERE fingerprint=$1) RETURNING id",
            f"node_alert:{dev['id']}:{i['key']}:{date.today().isoformat()}", f"{dev['name']}: {i['text'][0].lower()}{i['text'][1:]}",
            f"{i['text']}. " + (advice or f"Ask me \"why is the {dev['name']} slow\" or \"how's the {dev['name']} doing\" "
                                          "for the details."),
            json.dumps({"device_id": dev["id"], "key": i["key"]}), "Nothing changes; it only lets me know you've seen it.")
        made = made or bool(item)
    if made:
        log.warning("node %s: %s", dev["id"], "; ".join(i["text"] for i in urgent))
        await mailbox.notify(redis)


@router.post("/nodes/hello")
async def hello(body: Hello, request: Request):
    dev = await node_from(request)
    info = auth.clean_info(body.info)
    if body.specs:
        info["specs"] = clean_tree(body.specs)
    if body.vitals:
        v = clean_tree(body.vitals)
        redis = request.app.state.redis
        if isinstance(v.get("cpu"), (int, float)):
            await redis.lpush(RECENT.format(dev["id"]), v["cpu"])
            await redis.ltrim(RECENT.format(dev["id"]), 0, 9)
            await redis.expire(RECENT.format(dev["id"]), 86400)
        recent = [float(x) for x in await redis.lrange(RECENT.format(dev["id"]), 0, 9)]
        found = issues(v, recent, health_rules(request.app))
        info.update(vitals=v, issues=found, vitals_at=int(time.time()))
        urgent = [i for i in found if i["level"] == "urgent"]
        if urgent:
            try:
                await _alert(request.app.state.redis, dev, urgent)
            except Exception:
                log.exception("couldn't file a health alert for node %s", dev["id"])
    await db.pool().execute("UPDATE devices SET info = info || $2::jsonb, last_seen=now() WHERE id=$1",
                            dev["id"], json.dumps(info))
    await request.app.state.redis.set(ONLINE.format(dev["id"]), "1", ex=offline_after(request.app))
    log.info("node %s (%s) says hello: %s, agent %s", dev["id"], dev["name"], info.get("os"), info.get("agent_version"))
    out = {"id": dev["id"], "name": dev["name"], "poll_seconds": max(5, offline_after(request.app) - 20)}
    rules = update_rules(request.app)
    if rules["automatic"]:
        out["agent"] = {**agent_release(), "max_attempts": rules["max_attempts"]}
        if await request.app.state.redis.getdel(RETRY.format(dev["id"])):
            out["retry_update"] = True                  # he pressed Retry on the mailbox card
    return out


@router.post("/nodes/screen")
async def screen(request: Request):
    """A one-time link that signs this node's own kiosk browser in as its screen."""
    dev = await node_from(request)
    return {"login": await auth.screen_login(request.app.state.redis, dev)}


class UpdateFailed(BaseModel):
    version: str
    error: str = ""
    attempts: int = 0


@router.post("/nodes/update-failed")
async def update_failed(body: UpdateFailed, request: Request):
    """After max_attempts, the node stops trying that version and he hears about it once."""
    from . import mailbox
    dev = await node_from(request)
    running = (await db.pool().fetchval("SELECT info->>'agent_version' FROM devices WHERE id=$1", dev["id"])) or "its current version"
    version, error = body.version[:40], " ".join(body.error.split())[:300] or "no reason given"
    item = await db.pool().fetchval(
        "INSERT INTO mailbox_items (kind, fingerprint, source, title, summary, proposal, evidence, strength, confidence, "
        "effect, urgent) SELECT 'node_update', $1, 'constellation', $2, $3, $4::jsonb, $5::jsonb, 1, 1, $6, true "
        "WHERE NOT EXISTS (SELECT 1 FROM mailbox_items WHERE fingerprint=$1 AND status IN ('waiting','deferred')) RETURNING id",
        f"node_update:{dev['id']}:{version}", f"{dev['name']} couldn't update its agent",
        f"It tried version {version} {body.attempts or 'several'} times and stopped. It's still working on {running}.",
        json.dumps({"device_id": dev["id"], "version": version}), json.dumps([f"Last error: {error}"]),
        "It tries the update again at its next check-in (within a minute).")
    if item:
        log.warning("node %s gave up updating to %s: %s", dev["id"], version, error)
        await mailbox.notify(request.app.state.redis)
    return {"ok": True}


@router.get("/nodes/commands")
async def commands(request: Request, wait: int = 25):
    """Waits up to `wait` seconds for a command. Each call also marks the node online."""
    dev = await node_from(request)
    redis = request.app.state.redis
    await redis.set(ONLINE.format(dev["id"]), "1", ex=offline_after(request.app))
    wait = max(0, min(wait, offline_after(request.app) - 15))
    got = await redis.blpop([QUEUE.format(dev["id"])], timeout=wait) if wait else await redis.lpop(QUEUE.format(dev["id"]))
    raw = got[1] if isinstance(got, (list, tuple)) else got
    if not raw:
        return {"commands": []}
    cmd = json.loads(raw)
    if time.time() - cmd["at"] > COMMAND_TTL:
        log.info("dropping stale command %s for node %s", cmd["id"], dev["id"])
        return {"commands": []}
    return {"commands": [cmd]}


class CommandResult(BaseModel):
    ok: bool
    data: dict[str, Any] = {}
    error: str | None = None


@router.post("/nodes/commands/{cmd_id}/result")
async def command_result(cmd_id: str, body: CommandResult, request: Request):
    dev = await node_from(request)
    redis = request.app.state.redis
    key = RESULT.format(cmd_id)
    await redis.rpush(key, json.dumps({"node": dev["id"], **body.model_dump()}))
    await redis.expire(key, COMMAND_TTL)
    return {"ok": True}


# ---- the live view: his screen, as it is now -----------------------------------------------------
#
# While a display shows a node's live panel, it says so every few seconds (want, 12 s). The node
# waits on its own channel (not the command queue, so it works while a sign-in is running) and
# sends a few frames a second for as long as someone is watching. A frame lives 10 s in Redis and
# nowhere else: never stored, never indexed, never shown to the model.

LIVE_WANT = "aurelia:live:{}:want"
LIVE_START = "aurelia:live:{}:start"
LIVE_FRAME = "aurelia:live:{}:frame"
LIVE_META = "aurelia:live:{}:meta"
LIVE_KEEP_SECONDS = 12
LIVE_FRAME_SECONDS = 10
LIVE_MAX_BYTES = 2_000_000


async def want_live(redis, node_id: int) -> None:
    await redis.set(LIVE_WANT.format(node_id), "1", ex=LIVE_KEEP_SECONDS)
    if not await redis.exists(LIVE_META.format(node_id)):        # not streaming yet: wake it
        await redis.rpush(LIVE_START.format(node_id), "1")
        await redis.ltrim(LIVE_START.format(node_id), -1, -1)
        await redis.expire(LIVE_START.format(node_id), 30)


async def live_state(redis, node_id: int) -> dict[str, Any]:
    raw = await redis.get(LIVE_META.format(node_id))
    meta = json.loads(raw) if raw else {}
    if meta.get("at"):
        meta["age"] = round(max(0.0, time.time() - meta.pop("at")), 1)
    return meta


@router.get("/nodes/live/wait")
async def live_wait(request: Request, wait: int = 25):
    """The node's second channel: returns as soon as someone wants to watch its screen."""
    dev = await node_from(request)
    redis = request.app.state.redis
    want = LIVE_WANT.format(dev["id"])
    if await redis.exists(want):
        await redis.delete(LIVE_START.format(dev["id"]))
        return {"want": True}
    wait = max(0, min(wait, offline_after(request.app) - 15))
    got = await redis.blpop([LIVE_START.format(dev["id"])], timeout=wait) if wait else None
    return {"want": bool(got) and bool(await redis.exists(want))}


class LiveFrame(BaseModel):
    jpeg: str = ""
    w: int = 0
    h: int = 0
    title: str = ""
    site: str = ""
    problem: str = ""


@router.post("/nodes/live/frame")
async def live_frame(body: LiveFrame, request: Request):
    dev = await node_from(request)
    redis = request.app.state.redis
    meta = {"at": time.time(), "w": body.w, "h": body.h, "title": " ".join(body.title.split())[:120],
            "site": body.site[:80], "problem": " ".join(body.problem.split())[:200]}
    if body.jpeg:
        try:
            raw = base64.b64decode(body.jpeg, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(400, "not a picture")
        if len(raw) > LIVE_MAX_BYTES or not raw.startswith(b"\xff\xd8"):
            raise HTTPException(400, "not a JPEG, or too big")
        await redis.set(LIVE_FRAME.format(dev["id"]), raw, ex=LIVE_FRAME_SECONDS)
        meta["problem"] = ""
    await redis.set(LIVE_META.format(dev["id"]), json.dumps(meta), ex=LIVE_FRAME_SECONDS)
    return {"keep": bool(await redis.exists(LIVE_WANT.format(dev["id"])))}


# ---- Aurelia's side ------------------------------------------------------------------------

async def online(app, node_id: int) -> bool:
    return bool(await app.state.redis.exists(ONLINE.format(node_id)))


async def send(app, node_id: int, kind: str, args: dict[str, Any], timeout: float = 20) -> dict[str, Any]:
    """Queue a command for a node and wait for its answer."""
    cmd_id = uuid.uuid4().hex
    redis = app.state.redis
    await redis.rpush(QUEUE.format(node_id), json.dumps({"id": cmd_id, "kind": kind, "args": args, "at": time.time()}))
    await redis.expire(QUEUE.format(node_id), COMMAND_TTL)
    got = await redis.blpop([RESULT.format(cmd_id)], timeout=max(1, int(timeout)))
    if not got:
        raise TimeoutError("it didn't answer in time")
    res = json.loads(got[1])
    if res["node"] != node_id:                         # never trust an answer from another node
        raise RuntimeError("the answer came from the wrong node")
    return res


# ---- the agent itself (no key needed) --------------------------------------------------------------

@router.get("/node/agent.py", include_in_schema=False)
async def agent_file():
    return PlainTextResponse((AGENT_DIR / "aurelia_node.py").read_text(encoding="utf-8"),
                             headers={"Cache-Control": "no-store"})


@router.get("/node/guard.py", include_in_schema=False)
async def guard_file():
    return PlainTextResponse((AGENT_DIR / "aurelia_node_guard.py").read_text(encoding="utf-8"),
                             headers={"Cache-Control": "no-store"})


@router.get("/node/install-linux.sh", include_in_schema=False)
async def linux_installer():
    return PlainTextResponse((AGENT_DIR / "install-linux.sh").read_text(encoding="utf-8"),
                             headers={"Cache-Control": "no-store"})
