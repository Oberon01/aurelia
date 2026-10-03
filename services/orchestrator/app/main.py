import asyncio
import hashlib
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from redis.asyncio import Redis

from . import (admiral, ambient, assistant, auth, constellation, push, quasar, db, digest, glance, learning, llm_api, mailbox, memory, mnemosyne,
               nodes, registry, releases, tasks)
from .config import ConfigStore
from .events import CHANNEL
from .governance import GovernanceStore
from .llm.router import Router
from .settings import settings

logging.basicConfig(level=logging.INFO, format='{"ts":"%(asctime)s","lvl":"%(levelname)s","log":"%(name)s","msg":"%(message)s"}')
log = logging.getLogger("aurelia")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.config = ConfigStore(settings.config_path)
    watcher = app.state.config.start()
    app.state.governance = GovernanceStore(settings.governance_path)
    gov_watcher = app.state.governance.start()
    app.state.llm = Router(app.state.config)
    app.state.embedder = memory.Embedder(app.state.config)
    # redis-py 8 reads with a 5 s timeout by default; node long-polls wait up to 30 s.
    app.state.redis = Redis.from_url(settings.redis_url, socket_timeout=60)
    await db.connect(settings.database_url)
    app.state.draining = False
    maintenance = asyncio.create_task(memory.maintenance_loop(app))
    record = asyncio.create_task(mnemosyne.record_loop(app))
    mnemo = asyncio.create_task(mnemosyne.register_loop(app))
    stars = asyncio.create_task(constellation.register_loop(app))
    media = asyncio.create_task(quasar.register_loop(app))
    pushes = asyncio.create_task(push.mailbox_loop(app))
    plans = asyncio.create_task(admiral.register_loop(app))
    log.info("orchestrator ready")
    try:
        yield
    finally:
        # Graceful exit: fail readiness first so the proxy stops sending traffic,
        # then close connections once in-flight requests have finished.
        app.state.draining = True
        watcher.cancel()
        gov_watcher.cancel()
        maintenance.cancel()
        record.cancel()
        mnemo.cancel()
        stars.cancel()
        media.cancel()
        pushes.cancel()
        plans.cancel()
        await db.close()
        await app.state.redis.aclose()
        log.info("orchestrator stopped")


app = FastAPI(title="Aurelia Orchestrator", version="0.2.0", lifespan=lifespan)
app.include_router(tasks.router)
app.include_router(assistant.router)
app.include_router(memory.router)
app.include_router(releases.router)
app.include_router(mailbox.router)
app.include_router(digest.router)
app.include_router(mnemosyne.router)
app.include_router(glance.router)
app.include_router(ambient.router)
app.include_router(llm_api.router)
app.include_router(learning.router)
app.include_router(auth.router)
app.include_router(constellation.router)
app.include_router(nodes.router)
app.include_router(quasar.router)
app.include_router(push.router)
app.include_router(quasar.node_router)
app.include_router(admiral.router)


VIEW = Path(__file__).parent / "static" / "view.html"


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/view")


def _view() -> tuple[str, str]:
    html = VIEW.read_text()
    return html, hashlib.sha256(html.encode()).hexdigest()[:12]


@app.get("/view", include_in_schema=False)
async def view(request: Request):
    """The surgical view: a page that only renders panel events from /ws and
    sends what you type to /ask. It holds no logic of its own. It carries its
    version so it can reload itself when a newer one goes live."""
    html, version = _view()
    out = HTMLResponse(html.replace("__VIEW_VERSION__", version), headers={"Cache-Control": "no-cache"})
    key = request.cookies.get(auth.COOKIE)
    if key and await auth.device_for(key):
        auth.set_cookie(out, key)              # a paired browser stays paired as long as it's used
    return out


# ---- the phone app: installable (manifest, icons) and its service worker (notifications only) ---------

STATIC = Path(__file__).parent / "static"
MANIFEST = {"name": "Aurelia", "short_name": "Aurelia", "id": "/view", "start_url": "/view", "scope": "/",
            "display": "standalone", "background_color": "#04121a", "theme_color": "#04121a",
            "icons": [{"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                      {"src": "/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}
ICONS = {"icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"}


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    from fastapi.responses import JSONResponse
    return JSONResponse(MANIFEST, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    from fastapi.responses import Response
    return Response((STATIC / "sw.js").read_text(encoding="utf-8"), media_type="text/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/icons/{name}", include_in_schema=False)
async def icon(name: str):
    from fastapi import HTTPException
    from fastapi.responses import Response
    if name not in ICONS:
        raise HTTPException(404)
    return Response((STATIC / "icons" / name).read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=604800"})


@app.get("/view/version", include_in_schema=False)
async def view_version():
    return {"version": _view()[1]}


@app.get("/health")
async def health():
    """Liveness: the process is up."""
    return {"status": "ok"}


@app.get("/ready")
async def ready(request: Request):
    """Readiness: dependencies reachable. Traefik only routes to ready containers."""
    if request.app.state.draining:
        raise HTTPException(503, "draining")
    try:
        await request.app.state.redis.ping()
        await db.pool().fetchval("SELECT 1")
    except Exception as exc:
        raise HTTPException(503, f"not ready: {exc}")
    return {"status": "ready"}


# ---- function registry ---------------------------------------------------

@app.post("/registry/functions", status_code=204)
async def register_function(fn: registry.FunctionRegistration, request: Request):
    ttl = request.app.state.config.current.registry.function_ttl_seconds
    await registry.register(request.app.state.redis, fn, ttl)


@app.delete("/registry/functions/{name}", status_code=204)
async def unregister_function(name: str, request: Request):
    await registry.unregister(request.app.state.redis, name)


@app.get("/registry/functions")
async def list_functions(request: Request):
    return await registry.list_all(request.app.state.redis)


@app.get("/registry/seen")
async def functions_seen(request: Request):
    """Every function that has ever registered, with its last heartbeat."""
    return await registry.seen(request.app.state.redis)


@app.get("/config")
async def current_config(request: Request):
    return request.app.state.config.current


class ConfigCheck(BaseModel):
    path: str          # repo-relative, e.g. "config/aurelia.yaml"
    content: str


@app.post("/config/validate")
async def validate_config(body: ConfigCheck):
    """Check a config file against the schema this orchestrator enforces, before
    it's applied. Chrysalis calls this for every changed config file."""
    from .config import RuntimeConfig
    from .governance import Charter, FunctionPolicy
    import re
    models = {"config/aurelia.yaml": RuntimeConfig, "config/governance/charter.yaml": Charter}
    model = models.get(body.path)
    if model is None and re.fullmatch(r"config/governance/functions/[\w-]+\.yaml", body.path):
        model = FunctionPolicy
    if model is None:
        return {"valid": True, "checked": False}
    try:
        data = yaml.safe_load(body.content) or {}
        model.model_validate(data)
        if model is RuntimeConfig:          # every fast-path regex must compile
            for p in data.get("fastpath", {}).get("patterns", []):
                re.compile(p["match"])
    except Exception as exc:
        return {"valid": False, "checked": True, "error": str(exc).splitlines()[0:4]}
    return {"valid": True, "checked": True}


# ---- governance (read-only over HTTP; changed only by editing the files) ---

@app.get("/governance/charter")
async def governance_charter(request: Request):
    return request.app.state.governance.charter


@app.get("/governance/functions")
async def governance_functions(request: Request):
    """Every function that has a policy, running or not (read-only)."""
    gov = request.app.state.governance
    return [{"function": name, "enabled": p.enabled, "tools": {t: tp.tier for t, tp in p.tools.items()},
             "rate_limit_per_minute": gov.rate_limit(name)} for name, p in sorted(gov.functions.items())]


@app.get("/governance/functions/{name}")
async def governance_function(name: str, request: Request):
    """Each function fetches its own rules here and enforces them internally."""
    policy = request.app.state.governance.for_function(name)
    if policy is None:
        raise HTTPException(404, f"no policy for function '{name}'")
    return policy


# ---- display event stream -------------------------------------------------

@app.websocket("/ws")
async def events_ws(ws: WebSocket):
    """Streams panel events to the display. Clients reconnect automatically,
    so an orchestrator swap shows up as a brief reconnect, not an outage.
    Each display gets only its own panels (events.for_display); a revoked device's
    connection is closed within KEY_CACHE_SECONDS."""
    from . import auth
    from .events import decode, for_display
    me = await auth.asker(ws.headers)
    if ws.headers.get("x-aurelia-device") and me is None:
        await ws.close(code=4401)              # revoked between the proxy's check and now
        return
    await ws.accept()
    redis = ws.app.state.redis
    pubsub = redis.pubsub()
    await pubsub.subscribe(CHANNEL)
    checked = 0.0
    try:
        if me is not None:                     # what was sent here while it wasn't open
            from .constellation import DISPLAY_QUEUE
            q = DISPLAY_QUEUE.format(me["id"])
            for raw in await redis.lrange(q, 0, -1):
                await ws.send_text(raw.decode() if isinstance(raw, bytes) else raw)
            await redis.delete(q)
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg is not None:
                text = msg["data"].decode() if isinstance(msg["data"], bytes) else msg["data"]
                try:
                    wanted = for_display(decode(text), me)
                except ValueError:
                    wanted = False
                if wanted:
                    await ws.send_text(text)
            if me is not None and time.monotonic() - checked > 5:
                checked = time.monotonic()
                if await auth.device_by_id(me["id"]) is None:
                    await ws.close(code=4401)
                    break
                from .constellation import DISPLAY_SEEN
                await redis.set(DISPLAY_SEEN.format(me["id"]), "1", ex=20)   # "open now", for send
                me = await auth.device_by_id(me["id"]) or me                  # its role may have changed
            await asyncio.sleep(0)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await pubsub.unsubscribe(CHANNEL)
        await pubsub.aclose()
