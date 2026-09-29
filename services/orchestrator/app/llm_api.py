"""Model calls and embeddings for functions (Kepler first).

Functions never hold an API key. They ask the orchestrator, which picks the
provider from config (llm.routes), enforces the monthly cloud budget, and logs
every call, so everything Aurelia spends shows up in one place.
"""
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import db
from .llm.base import Message, ProviderError

router = APIRouter(prefix="/llm", tags=["llm"])
ROUTES = {"research", "summarize", "chat"}          # purposes a function may ask for


class Turn(BaseModel):
    role: str                                        # user | assistant
    text: str


class CompleteBody(BaseModel):
    purpose: str = "research"
    system: str = ""
    messages: list[Turn]
    function: str | None = None                      # who's asking, for the log


class EmbedBody(BaseModel):
    texts: list[str]


@router.post("/complete")
async def complete(body: CompleteBody, request: Request):
    from .assistant import CLOUD_PROVIDERS, _cloud_tokens_this_month
    app = request.app
    if body.purpose not in ROUTES:
        raise HTTPException(400, f"unknown purpose '{body.purpose}'")
    if not body.messages or any(m.role not in ("user", "assistant") for m in body.messages):
        raise HTTPException(400, "messages must be user/assistant turns")
    llm = app.state.config.current.llm
    order = llm.routes.get(body.purpose) or [llm.default_provider]
    if any(p in CLOUD_PROVIDERS for p in order) and await _cloud_tokens_this_month() >= llm.monthly_token_cap:
        raise HTTPException(429, "This month's model budget is used up.")
    started = time.monotonic()
    try:
        c = await app.state.llm.complete(body.purpose, body.system, [Message(m.role, m.text) for m in body.messages], [])
    except ProviderError as exc:
        raise HTTPException(503, f"No model available right now ({exc}).")
    await db.pool().execute(
        "INSERT INTO llm_calls (request_id, provider, model, input_tokens, output_tokens, cache_read_tokens, latency_ms) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)", None, c.provider, c.model, c.usage.input_tokens, c.usage.output_tokens,
        c.usage.cache_read_tokens, int((time.monotonic() - started) * 1000))
    return {"text": c.text, "model": c.model, "provider": c.provider,
            "usage": {"input": c.usage.input_tokens, "output": c.usage.output_tokens}}


@router.post("/embed")
async def embed(body: EmbedBody, request: Request):
    """Local embeddings (Ollama). null when they're unavailable, so callers fall back to words."""
    texts = [t[:4000] for t in body.texts[:64]]
    vecs = await request.app.state.embedder.embed(texts) if request.app.state.embedder.enabled and texts else None
    return {"embeddings": vecs}

