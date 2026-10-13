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
NAMES = "aurelia:functions:names"     # the names registered (each registration still expires on its own key)
_parsed: dict[str, tuple[bytes, "FunctionRegistration"]] = {}   # name → (its JSON, parsed once per change)


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
    # "choice": the model decides per call between a panel and a brief answer (_display).
    # "panel": always opens a panel (the result is something to look at, e.g. a library or a quiz).
    display: str = "choice"


class FunctionRegistration(BaseModel):
    name: str                           # e.g. "nebula"
    description: str
    url: str                            # where the orchestrator reaches this function
    tools: list[ToolSpec]
    # "lagrange": a service connected through Lagrange (an MCP server, Home Assistant, a webhook). Governed
    # like any function, but with no rules file of its own until he writes one: every tool asks first.
    via: str | None = None


class ResolvedTool(BaseModel):
    function: str
    url: str
    tool: ToolSpec

    @property
    def qualified_name(self) -> str:
        return f"{self.function}.{self.tool.name}"


async def register(redis: Redis, fn: FunctionRegistration, ttl: int) -> None:
    from datetime import datetime, timezone
    async with redis.pipeline(transaction=False) as p:
        p.set(PREFIX + fn.name, fn.model_dump_json(), ex=ttl)
        p.sadd(NAMES, fn.name)
        p.hset(SEEN_KEY, fn.name, datetime.now(timezone.utc).isoformat())
        await p.execute()


def _parse(name: str, raw) -> "FunctionRegistration":
    raw = raw if isinstance(raw, bytes) else str(raw).encode()
    got = _parsed.get(name)
    if got is not None and got[0] == raw:
        return got[1]
    fn = FunctionRegistration.model_validate(json.loads(raw))
    _parsed[name] = (raw, fn)
    return fn


async def seen(redis: Redis) -> dict[str, str]:
    """When each function last checked in, including ones that aren't running now."""
    raw = await redis.hgetall(SEEN_KEY)
    return {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v) for k, v in raw.items()}


async def unregister(redis: Redis, name: str) -> bool:
    await redis.srem(NAMES, name)
    return bool(await redis.delete(PREFIX + name))


async def get_function(redis: Redis, name: str) -> FunctionRegistration | None:
    raw = await redis.get(PREFIX + name)
    return _parse(name, raw) if raw else None


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
    """Every function registered right now: its names in one set, their registrations in one MGET (never a scan
    of the whole keyspace). One whose registration expired is dropped from the set."""
    names = sorted((n.decode() if isinstance(n, bytes) else n) for n in await redis.smembers(NAMES))
    if not names and not await redis.exists(NAMES):          # (first start after an upgrade: find them once)
        async for key in redis.scan_iter(match=PREFIX + "*", count=500):
            k = key.decode() if isinstance(key, bytes) else key
            names.append(k[len(PREFIX):])
        if names:
            await redis.sadd(NAMES, *names)
        names.sort()
    if not names:
        return []
    raws = await redis.mget([PREFIX + n for n in names])
    gone = [n for n, raw in zip(names, raws) if not raw]
    if gone:
        await redis.srem(NAMES, *gone)
    return [_parse(n, raw) for n, raw in zip(names, raws) if raw]


async def connections(redis: Redis) -> set[str]:
    """The functions that are services connected through Lagrange, as registered right now."""
    return {f.name for f in await list_all(redis) if f.via == "lagrange"}
