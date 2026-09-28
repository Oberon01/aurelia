import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis

from . import db, registry, tasks
from .config import ConfigStore
from .events import CHANNEL
from .governance import GovernanceStore
from .settings import settings

logging.basicConfig(level=logging.INFO, format='{"ts":"%(asctime)s","lvl":"%(levelname)s","log":"%(name)s","msg":"%(message)s"}')
log = logging.getLogger("aurelia")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.config = ConfigStore(settings.config_path)
    watcher = app.state.config.start()
    app.state.governance = GovernanceStore(settings.governance_path)
    gov_watcher = app.state.governance.start()
    app.state.redis = Redis.from_url(settings.redis_url)
    await db.connect(settings.database_url)
    app.state.draining = False
    log.info("orchestrator ready")
    try:
        yield
    finally:
        # Graceful exit: fail readiness first so the proxy stops sending traffic,
        # then close connections once in-flight requests have finished.
        app.state.draining = True
        watcher.cancel()
        gov_watcher.cancel()
        await db.close()
        await app.state.redis.aclose()
        log.info("orchestrator stopped")


app = FastAPI(title="Aurelia Orchestrator", version="0.1.0", lifespan=lifespan)
app.include_router(tasks.router)


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


@app.get("/config")
async def current_config(request: Request):
    return request.app.state.config.current


# ---- governance (read-only over HTTP; changed only by editing the files) ---

@app.get("/governance/charter")
async def governance_charter(request: Request):
    return request.app.state.governance.charter


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
    so an orchestrator swap shows up as a brief reconnect, not an outage."""
    await ws.accept()
    pubsub = ws.app.state.redis.pubsub()
    await pubsub.subscribe(CHANNEL)
    try:
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg is not None:
                await ws.send_text(msg["data"].decode() if isinstance(msg["data"], bytes) else msg["data"])
            await asyncio.sleep(0)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await pubsub.unsubscribe(CHANNEL)
        await pubsub.aclose()
