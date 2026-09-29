"""Dynamic function registry, kept in Redis.

A *function* is a containerized service that groups related tools under a
space-themed name (Nebula = web, Probe = diagnostics, …). Functions register
themselves with all their tools and heartbeat. Each registration expires after
the configured TTL, so a stopped function disappears on its own and a new
version replaces the old one without the orchestrator restarting.

Tools are addressed as "<function>.<tool>", e.g. "probe.echo" or "nebula.search".
"""
import json

from pydantic import BaseModel
from redis.asyncio import Redis

PREFIX = "aurelia:function:"
SEEN_KEY = "aurelia:functions:seen"   # every function that has ever registered → last heartbeat (never expires)


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict                  # JSON schema for its arguments (used by the LLM in Phase 1)
    progress_unit: str | None = None    # "bytes", "files", "devices", "steps"… None = no measurable quantity
    freshness_seconds: int | None = None
    cancellable: bool = True
    # Look-ups this action changes, e.g. orbit.lock → ["orbit.status"]. The display
    # refreshes those panels in place when the action finishes, if they're open.
    refreshes: list[str] = []


class FunctionRegistration(BaseModel):
    name: str                           # e.g. "nebula"
    description: str
    url: str                            # where the orchestrator reaches this function
    tools: list[ToolSpec]


class ResolvedTool(BaseModel):
    function: str
    url: str
    tool: ToolSpec

    @property
    def qualified_name(self) -> str:
        return f"{self.function}.{self.tool.name}"


async def register(redis: Redis, fn: FunctionRegistration, ttl: int) -> None:
    from datetime import datetime, timezone
    await redis.set(PREFIX + fn.name, fn.model_dump_json(), ex=ttl)
    await redis.hset(SEEN_KEY, fn.name, datetime.now(timezone.utc).isoformat())


async def seen(redis: Redis) -> dict[str, str]:
    """When each function last checked in, including ones that aren't running now."""
    raw = await redis.hgetall(SEEN_KEY)
    return {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v) for k, v in raw.items()}


async def unregister(redis: Redis, name: str) -> bool:
    return bool(await redis.delete(PREFIX + name))


async def get_function(redis: Redis, name: str) -> FunctionRegistration | None:
    raw = await redis.get(PREFIX + name)
    return FunctionRegistration.model_validate(json.loads(raw)) if raw else None


async def resolve(redis: Redis, qualified: str) -> ResolvedTool | None:
    """'probe.echo' → the Probe function's URL plus the echo tool's spec."""
    fn_name, _, tool_name = qualified.partition(".")
    fn = await get_function(redis, fn_name)
    if fn is None:
        return None
    for tool in fn.tools:
        if tool.name == tool_name:
            return ResolvedTool(function=fn.name, url=fn.url, tool=tool)
    return None


async def list_all(redis: Redis) -> list[FunctionRegistration]:
    functions = []
    async for key in redis.scan_iter(match=PREFIX + "*"):
        raw = await redis.get(key)
        if raw:
            functions.append(FunctionRegistration.model_validate(json.loads(raw)))
    return sorted(functions, key=lambda f: f.name)
