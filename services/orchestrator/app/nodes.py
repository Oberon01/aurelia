"""The node connection (Phase 7 step 2): how a computer running Aurelia's node agent
talks to her.

A node never opens a port. It calls out, over the tailnet, with its own key:
  POST /nodes/hello                  what it is (OS, version, screens, what it can do)
  GET  /nodes/commands?wait=25       waits for work (long poll); this is also its heartbeat
  POST /nodes/commands/{id}/result   how a command went
Commands are queued per node in Redis. A node is online while it keeps polling
(offline_after_seconds, from Constellation's policy).

Keys are checked here as well as at the proxy: only an approved *node* key reaches
these endpoints, and each node only ever sees its own queue.

Serving the agent itself (/node/agent.py, /node/install-linux.sh) needs no key: it's
code, no secrets, and a machine can't have a key before it pairs.
"""
import asyncio
import json
import logging
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
COMMAND_TTL = 120                     # a command nobody picked up in 2 minutes is dropped, never run late
DEFAULT_OFFLINE_AFTER = 45
AGENT_DIR = Path(__file__).resolve().parent / "node_agent"


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


@router.post("/nodes/hello")
async def hello(body: Hello, request: Request):
    dev = await node_from(request)
    info = auth.clean_info(body.info)
    await db.pool().execute("UPDATE devices SET info = info || $2::jsonb, last_seen=now() WHERE id=$1",
                            dev["id"], json.dumps(info))
    await request.app.state.redis.set(ONLINE.format(dev["id"]), "1", ex=offline_after(request.app))
    log.info("node %s (%s) says hello: %s", dev["id"], dev["name"], info.get("os"))
    return {"id": dev["id"], "name": dev["name"], "poll_seconds": max(5, offline_after(request.app) - 20)}


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


@router.get("/node/install-linux.sh", include_in_schema=False)
async def linux_installer():
    return PlainTextResponse((AGENT_DIR / "install-linux.sh").read_text(encoding="utf-8"),
                             headers={"Cache-Control": "no-store"})
