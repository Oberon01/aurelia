"""POST /ask — Aurelia understanding a request.

1. Fast path: a configured pattern matches → run the tool directly, templated reply.
2. Otherwise the LLM chooses tools. Every tool call goes through the same
   registry + governance + rate-limit path as POST /tasks; the model never
   gets a shortcut around the rules.
3. "change"-tier actions are held as pending and returned for confirmation;
   POST /ask/confirm runs (or discards) them.

Conversation state lives in Redis (stateless replicas), and every request and
model call is logged for timing and cost analysis.
"""
import json
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import db, fastpath, registry
from .llm.base import Message, ProviderError, ToolDef
from .settings import settings
from .tasks import StartTask, launch, wait_for_result

router = APIRouter(prefix="/ask", tags=["assistant"])

SESSION_PREFIX = "aurelia:session:"
PENDING_PREFIX = "aurelia:pending:"
PENDING_TTL = 600
CLOUD_PROVIDERS = ("claude",)


class Ask(BaseModel):
    text: str
    session_id: str | None = None
    user: str = "owner"


class Confirm(BaseModel):
    action_id: str
    approve: bool
    user: str = "owner"


def api_name(qualified: str) -> str:
    return qualified.replace(".", "__")


def qualified_name(api: str) -> str:
    return api.replace("__", ".", 1)


# ---- endpoints --------------------------------------------------------------

@router.post("")
async def ask(body: Ask, request: Request):
    app = request.app
    cfg = app.state.config.current
    started = time.monotonic()
    request_id = str(uuid.uuid4())
    session_id = body.session_id or uuid.uuid4().hex
    history = await _load_session(app, session_id)

    outcome: dict[str, Any] = {"route": "llm", "reply": "", "task_ids": [], "needs_confirmation": []}

    fp = fastpath.match(body.text, cfg.fastpath.patterns) if cfg.fastpath.enabled else None
    resolved = await registry.resolve(app.state.redis, fp.pattern.tool) if fp else None
    if fp and resolved:
        outcome = await _run_fastpath(app, body, fp, resolved)
    else:
        outcome = await _run_llm(app, body, history, request_id)

    latency = int((time.monotonic() - started) * 1000)
    await db.pool().execute(
        "INSERT INTO ask_requests (id, session_id, user_id, text, route, reply, task_ids, latency_ms) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7::uuid[], $8)",
        uuid.UUID(request_id), session_id, body.user, body.text, outcome["route"], outcome["reply"],
        [uuid.UUID(t) for t in outcome["task_ids"]], latency,
    )
    await _save_session(app, session_id, history + [Message("user", body.text),
                                                    Message("assistant", outcome["reply"])])
    return {"request_id": request_id, "session_id": session_id, "latency_ms": latency, **outcome}


@router.post("/confirm")
async def confirm(body: Confirm, request: Request):
    app = request.app
    key = PENDING_PREFIX + body.action_id
    raw = await app.state.redis.get(key)
    if raw is None:
        raise HTTPException(404, "that action expired or was already handled")
    pending = json.loads(raw)
    if pending["user"] != body.user:
        raise HTTPException(403, "that action belongs to someone else")
    await app.state.redis.delete(key)
    if not body.approve:
        return {"reply": "Okay, I won't.", "task_ids": []}
    try:
        task_id = await launch(app, StartTask(tool=pending["tool"], args=pending["args"],
                                              user=body.user, confirmed=True))
    except HTTPException as exc:
        return {"reply": f"I couldn't do that: {_reason(exc)}", "task_ids": []}
    result = await wait_for_result(task_id, app.state.config.current.llm.wait_for_result_seconds)
    return {"reply": _summarize(pending["tool"], result), "task_ids": [task_id]}


# ---- fast path ----------------------------------------------------------------

async def _run_fastpath(app, body: Ask, fp, resolved) -> dict[str, Any]:
    args = fastpath.coerce(fp.args, resolved.tool.input_schema)
    out: dict[str, Any] = {"route": "fastpath", "reply": "", "task_ids": [], "needs_confirmation": []}
    try:
        task_id = await launch(app, StartTask(tool=resolved.qualified_name, args=args, user=body.user))
    except HTTPException as exc:
        if exc.status_code == 409:
            action = await _hold(app, resolved.qualified_name, args, body.user, _reason(exc))
            out["needs_confirmation"].append(action)
            out["reply"] = f"That needs your confirmation: {_reason(exc)}"
        else:
            out["reply"] = f"I can't do that: {_reason(exc)}"
        return out
    out["task_ids"].append(task_id)
    result = await wait_for_result(task_id, app.state.config.current.llm.wait_for_result_seconds)
    if result and result["status"] == "failed":
        out["reply"] = f"That didn't work: {result.get('error') or 'the tool failed'}"
    else:
        out["reply"] = fastpath.render_reply(fp.pattern, fp.groups)
    return out


# ---- LLM loop -----------------------------------------------------------------

async def _run_llm(app, body: Ask, history: list[Message], request_id: str) -> dict[str, Any]:
    cfg = app.state.config.current.llm
    out: dict[str, Any] = {"route": "llm", "reply": "", "task_ids": [], "needs_confirmation": []}

    if await _cloud_tokens_this_month() >= cfg.monthly_token_cap and _uses_cloud(cfg):
        out.update(route="error", reply="I've reached this month's model budget, so I can only run "
                                        "fast-path commands until it resets or the cap is raised.")
        return out

    tools, by_api, functions = await _tool_defs(app, body.user)
    system = _system_prompt(app, functions)
    messages = history + [Message("user", body.text)]

    for _ in range(cfg.max_tool_rounds + 1):
        started = time.monotonic()
        try:
            completion = await app.state.llm.complete("chat", system, messages, tools)
        except ProviderError as exc:
            out.update(route="error", reply=f"I can't reach a language model right now ({exc}).")
            return out
        await _log_llm_call(request_id, completion, started)

        if not completion.tool_calls:
            out["reply"] = completion.text or "Done."
            return out

        messages.append(Message("assistant", completion.text, tool_calls=completion.tool_calls))
        for call in completion.tool_calls:
            qualified = by_api.get(call.name)
            result_text, is_error = await _run_tool_call(app, body.user, qualified, call.args, out)
            messages.append(Message("tool", result_text, tool_call_id=call.id,
                                    tool_name=call.name, is_error=is_error))

    out["reply"] = "That took more steps than I'm allowed for one request, so I stopped."
    return out


async def _run_tool_call(app, user: str, qualified: str | None, args: dict, out: dict) -> tuple[str, bool]:
    if qualified is None:
        return _data({"status": "error", "error": "no such tool"}), True
    try:
        task_id = await launch(app, StartTask(tool=qualified, args=args, user=user))
    except HTTPException as exc:
        if exc.status_code == 409:
            action = await _hold(app, qualified, args, user, _reason(exc))
            out["needs_confirmation"].append(action)
            return _data({"status": "needs_confirmation", "reason": _reason(exc),
                          "note": "Tell the user what you were about to do. They confirm on the display."}), False
        return _data({"status": "refused", "reason": _reason(exc)}), True
    out["task_ids"].append(task_id)
    result = await wait_for_result(task_id, app.state.config.current.llm.wait_for_result_seconds)
    if result is None:
        return _data({"status": "running", "task_id": task_id,
                      "note": "Still running; progress is shown on the display."}), False
    return _data({"status": result["status"], "partial": result["partial"],
                  "result": result["result"], "error": result["error"]}), result["status"] == "failed"


def _data(payload: dict) -> str:
    """Tool output is wrapped so the model reads it as data, never as instructions."""
    return "<tool_output>\n" + json.dumps(payload, default=str) + "\n</tool_output>"


async def _tool_defs(app, user: str) -> tuple[list[ToolDef], dict[str, str], list[dict]]:
    """Tools the model may see: registered, enabled by policy, not blocked for this user.
    Also returns the functions those tools belong to, for the system prompt."""
    gov = app.state.governance
    defs, by_api, functions = [], {}, []
    for fn in await registry.list_all(app.state.redis):
        visible = []
        for tool in fn.tools:
            decision = gov.decide(fn.name, tool.name, user, confirmed=True)
            if not decision.allowed:
                continue
            q = f"{fn.name}.{tool.name}"
            note = " Requires the user's confirmation." if gov.decide(fn.name, tool.name, user, False).needs_confirmation else ""
            defs.append(ToolDef(name=api_name(q), description=f"[{fn.name.capitalize()}] {tool.description}{note}",
                                input_schema=tool.input_schema))
            by_api[api_name(q)] = q
            visible.append(tool.name)
        if visible:
            functions.append({"name": fn.name, "description": fn.description, "tools": visible})
    return sorted(defs, key=lambda d: d.name), by_api, functions


def _system_prompt(app, functions: list[dict]) -> str:
    identity_path = Path(settings.config_path).parent / "identity.md"
    try:
        identity = identity_path.read_text().strip()
    except FileNotFoundError:
        identity = "You are Aurelia, a personal assistant."
    principles = app.state.governance.charter.principles
    if principles:
        identity += "\n\nCharter principles:\n" + "\n".join(f"- {p}" for p in principles)
    # Live list of her own functions, so she can describe herself accurately.
    if functions:
        lines = [f"- {f['name'].capitalize()}: {f['description']} Tools: {', '.join(f['tools'])}." for f in functions]
        identity += "\n\nYour functions right now (online and permitted for this user):\n" + "\n".join(lines)
    else:
        identity += "\n\nNo functions are online right now, so you can talk but not act."
    return identity


# ---- helpers ------------------------------------------------------------------

async def _hold(app, tool: str, args: dict, user: str, reason: str) -> dict[str, Any]:
    action_id = uuid.uuid4().hex
    await app.state.redis.set(PENDING_PREFIX + action_id,
                              json.dumps({"tool": tool, "args": args, "user": user}), ex=PENDING_TTL)
    return {"action_id": action_id, "tool": tool, "args": args, "reason": reason}


def _reason(exc: HTTPException) -> str:
    d = exc.detail
    return d.get("reason", str(d)) if isinstance(d, dict) else str(d)


def _summarize(tool: str, result: dict | None) -> str:
    if result is None:
        return f"Started {tool}; it's still running."
    if result["status"] == "completed":
        return f"Done: {tool}."
    return f"{tool} ended as {result['status']}" + (f": {result['error']}" if result.get("error") else ".")


def _uses_cloud(cfg) -> bool:
    return any(p in CLOUD_PROVIDERS for p in cfg.routes.get("chat", [cfg.default_provider]))


async def _cloud_tokens_this_month() -> int:
    return await db.pool().fetchval(
        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM llm_calls "
        "WHERE provider = ANY($1::text[]) AND at >= date_trunc('month', now())", list(CLOUD_PROVIDERS)) or 0


async def _log_llm_call(request_id: str, completion, started: float) -> None:
    await db.pool().execute(
        "INSERT INTO llm_calls (request_id, provider, model, input_tokens, output_tokens, cache_read_tokens, latency_ms) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        uuid.UUID(request_id), completion.provider, completion.model, completion.usage.input_tokens,
        completion.usage.output_tokens, completion.usage.cache_read_tokens,
        int((time.monotonic() - started) * 1000),
    )


async def _load_session(app, session_id: str) -> list[Message]:
    raw = await app.state.redis.get(SESSION_PREFIX + session_id)
    if not raw:
        return []
    return [Message(m["role"], m["text"]) for m in json.loads(raw)]


async def _save_session(app, session_id: str, messages: list[Message]) -> None:
    cfg = app.state.config.current.session
    # Only user text and final replies are kept, so stored history is always valid to resend.
    kept = [{"role": m.role, "text": m.text} for m in messages if m.role in ("user", "assistant") and m.text]
    kept = kept[-cfg.max_turns * 2:]
    await app.state.redis.set(SESSION_PREFIX + session_id, json.dumps(kept), ex=cfg.ttl_minutes * 60)
