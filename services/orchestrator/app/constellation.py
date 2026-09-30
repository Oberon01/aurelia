"""Constellation: his devices and the machines Aurelia runs on (Phase 7).

Part of the orchestrator (like Mnemosyne), because devices authenticate here and
nodes hold their connection here (nodes.py). Tools:
  devices  everything paired, and which computers are online
  open     open a web page or an app on one of his computers
  apps     what's installed on one of them
Pairing and revoking happen on the display (or the server), never through a tool:
Aurelia can show the list but can't let a device in or lock one out.
"""
import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from urllib.parse import urlsplit

from . import auth, db, nodes, registry

log = logging.getLogger("aurelia.constellation")
router = APIRouter(prefix="/constellation", tags=["constellation"])
NAME = "constellation"

TOOLS = [
    registry.ToolSpec(
        name="devices", description="Every device paired with Aurelia (browsers, the phone, computers running "
                                    "her node, terminals): what it is, when it was last seen, and any waiting to pair. "
                                    "Revoking or approving happens on the display, not through you.",
        input_schema={"type": "object", "properties": {}}, cancellable=False, freshness_seconds=30, display="panel"),
    registry.ToolSpec(
        name="open", description="Open a web page (url) or an app (app, by name) on one of his computers running "
                                 "your node (target: its name, e.g. 'living room'). Leave target out when only one "
                                 "is online. Only http and https pages.",
        input_schema={"type": "object", "properties": {
            "target": {"type": "string", "description": "which computer, by name"},
            "url": {"type": "string"}, "app": {"type": "string", "description": "an installed app, by name"}}},
        cancellable=False),
    registry.ToolSpec(
        name="apps", description="The apps installed on one of his computers (target: its name).",
        input_schema={"type": "object", "properties": {"target": {"type": "string"}}},
        cancellable=False, freshness_seconds=300),
]


async def devices(app, user: str, args: dict) -> dict:
    items = await auth.listing()
    for d in items:
        if d["kind"] == "node" and d["status"] == "approved":
            d["online"] = await nodes.online(app, d["id"])
    live = [d for d in items if d["status"] == "approved"]
    waiting = [d for d in items if d["status"] == "pending"]
    head = f"{len(live)} paired" + (f" · {len(waiting)} waiting" if waiting else "")
    return {"headline": head, "paired": items}


def _match(asked: str, name: str, hostname: str | None) -> bool:
    """'living room', 'the living room tv', 'tv-pc' all find a node named 'Living room TV' (hostname tv-pc)."""
    from .auth import clean_name
    import re
    norm = lambda t: set(w for w in re.findall(r"[a-z0-9]+", t.lower()) if w not in ("the", "my", "on", "pc", "computer"))
    a = norm(clean_name(asked, ""))
    return bool(a) and (a <= norm(name) or a <= norm(hostname or "") or norm(name) <= a)


async def target(app, asked: str | None) -> dict:
    """The node a request means. Clear failures, never a guess between two machines."""
    rows = await db.pool().fetch("SELECT id, name, info FROM devices WHERE kind='node' AND status='approved'")
    ns = []
    for r in rows:
        info = r["info"] if isinstance(r["info"], dict) else json.loads(r["info"] or "{}")
        ns.append({"id": r["id"], "name": r["name"], "hostname": info.get("hostname"), "online": await nodes.online(app, r["id"])})
    if not ns:
        raise ValueError("None of your computers runs my node yet.")
    if asked and asked.strip():
        hits = [n for n in ns if _match(asked, n["name"], n["hostname"])]
        if not hits:
            raise ValueError(f"I don't know a computer called \"{asked}\". Yours: {', '.join(n['name'] for n in ns)}.")
        if len(hits) > 1:                              # e.g. re-paired without revoking the old entry
            live = [n for n in hits if n["online"]]
            if len(live) != 1:
                raise ValueError(f"Which one: {', '.join(n['name'] for n in (live or hits))}?")
            hits = live
        node = hits[0]
    else:
        live = [n for n in ns if n["online"]]
        if len(live) != 1:
            raise ValueError("Which computer? " + (f"Online: {', '.join(n['name'] for n in live)}." if live
                                                  else "None of them is online right now."))
        node = live[0]
    if not node["online"]:
        raise ValueError(f"{node['name']} isn't online right now.")
    return node


async def _ask_node(app, node: dict, kind: str, args: dict, timeout: float = 20) -> dict:
    try:
        res = await nodes.send(app, node["id"], kind, args, timeout)
    except TimeoutError:
        raise ValueError(f"{node['name']} didn't answer in time.")
    if not res.get("ok"):
        raise ValueError(f"{node['name']}: {res.get('error') or 'that did not work'}")
    return res.get("data") or {}


async def open_(app, user: str, args: dict) -> dict:
    url, name = str(args.get("url") or "").strip(), " ".join(str(args.get("app") or "").split())
    if bool(url) == bool(name):
        raise ValueError("Say a web page or an app to open (one of them).")
    if url:
        if "://" not in url:
            url = "https://" + url
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
            raise ValueError("I only open web pages (http or https) this way.")
    node = await target(app, args.get("target"))
    data = await _ask_node(app, node, "open_url" if url else "launch_app", {"url": url} if url else {"name": name})
    what = url or data.get("app") or name
    return {"headline": f"Opened on {node['name']}", "opened": {"node": node["name"], "what": what,
                                                               "kind": "page" if url else "app"}}


async def apps(app, user: str, args: dict) -> dict:
    node = await target(app, args.get("target"))
    data = await _ask_node(app, node, "list_apps", {}, 30)
    items = sorted(data.get("apps") or [], key=lambda a: str(a.get("name", "")).lower())
    return {"headline": f"{len(items)} apps on {node['name']}", "apps": {"node": node["name"], "items": items[:400]}}


HANDLERS = {"devices": devices, "open": open_, "apps": apps}


async def register_loop(app) -> None:
    from .settings import settings
    while True:
        ttl = app.state.config.current.registry.function_ttl_seconds
        try:
            await registry.register(app.state.redis, registry.FunctionRegistration(
                name=NAME, description="His devices: what's paired with Aurelia, and (soon) the computers she runs on.",
                url=f"{settings.self_url.rstrip('/')}/constellation", tools=TOOLS), ttl)
        except Exception:
            log.exception("couldn't register Constellation; will retry")
        await asyncio.sleep(max(3, ttl // 3))


@router.get("/health")
async def health(request: Request):
    """What Chrysalis and Probe check, like any function's /health."""
    try:
        rows = await db.pool().fetch("SELECT id, kind FROM devices WHERE status='approved'")
    except Exception as exc:
        return {"status": "error", "database": f"unreachable ({exc.__class__.__name__})"}
    ids = [r["id"] for r in rows if r["kind"] == "node"]
    live = sum([1 for i in ids if await nodes.online(request.app, i)])
    return {"status": "ok", "paired": len(rows), "computers": len(ids), "online": live}


class Invoke(BaseModel):
    task_id: str
    args: dict = {}


@router.post("/tools/{tool}/invoke", status_code=202)
async def invoke(tool: str, body: Invoke, request: Request):
    if tool not in HANDLERS:
        raise HTTPException(404, f"constellation has no tool '{tool}'")
    asyncio.create_task(_run(request.app, tool, body.task_id, body.args))
    return {"accepted": body.task_id}


@router.post("/cancel/{task_id}", status_code=202)
async def cancel(task_id: str):
    return {"cancelling": task_id}


async def _run(app, tool: str, task_id: str, args: dict) -> None:
    from . import tasks
    try:
        row = await db.pool().fetchrow("SELECT user_id FROM task_runs WHERE id=$1", uuid.UUID(task_id))
        data = await HANDLERS[tool](app, row["user_id"] if row else "owner", args)
        result = tasks.Result(status="completed", data=data)
    except ValueError as exc:
        result = tasks.Result(status="failed", error=str(exc))
    except Exception as exc:
        log.exception("constellation.%s failed", tool)
        result = tasks.Result(status="failed", error=f"Something went wrong: {exc}")
    await tasks._finish(tasks._AppContext(app), task_id, f"{NAME}.{tool}", result)
