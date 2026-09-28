import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from redis.asyncio import Redis

from . import assistant, db, memory, registry, tasks
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
    app.state.redis = Redis.from_url(settings.redis_url)
    await db.connect(settings.database_url)
    app.state.draining = False
    maintenance = asyncio.create_task(memory.maintenance_loop(app))
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
        await db.close()
        await app.state.redis.aclose()
        log.info("orchestrator stopped")


app = FastAPI(title="Aurelia Orchestrator", version="0.2.0", lifespan=lifespan)
app.include_router(tasks.router)
app.include_router(assistant.router)
app.include_router(memory.router)


VIEW = Path(__file__).parent / "static" / "view.html"


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/view")


@app.get("/view", include_in_schema=False)
async def view():
    """The surgical view: a page that only renders panel events from /ws and
    sends what you type to /ask. It holds no logic of its own."""
    return FileResponse(VIEW, media_type="text/html", headers={"Cache-Control": "no-cache"})


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
