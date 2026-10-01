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
import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import auth, context, db, fastpath, mnemosyne, profile, registry, style
from .llm.base import Message, ProviderError, ToolDef
from .settings import settings
from .tasks import StartTask, launch, wait_for_result

router = APIRouter(prefix="/ask", tags=["assistant"])
log = logging.getLogger("aurelia.assistant")

PENDING_PREFIX = "aurelia:pending:"
PENDING_TTL = 600
NEEDS_CONFIRMATION = "That needs your confirmation first."
CLOUD_PROVIDERS = ("claude",)


class Ask(BaseModel):
    text: str
    session_id: str | None = None
    user: str = "owner"
    screen: context.Screen | None = None     # the panels you can see, by number, and the one in focus
    device: int | None = None                # the display asking: set from the proxy's header, never by clients


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
    body.device = await auth.display_of(request.headers)       # its panels open on this display only
    # First thing you ask after the digest hour brings up the morning digest (once a day), right here.
    from . import digest
    asyncio.create_task(digest.maybe_show(app, body.user, body.device))
    request_id = str(uuid.uuid4())
    # The display makes its own session id, so two requests sent at once share one conversation.
    session_id = body.session_id if context.valid_session(body.session_id) else uuid.uuid4().hex
    history = await context.load(app.state.redis, session_id)

    outcome: dict[str, Any] = {"route": "llm", "reply": "", "task_ids": [], "needs_confirmation": []}

    chosen = style.switch(body.text)
    fp = fastpath.match(body.text, cfg.fastpath.patterns) if cfg.fastpath.enabled and chosen is None else None
    resolved = await registry.resolve(app.state.redis, fp.pattern.tool) if fp else None
    if chosen is not None:              # "use Raphael style" / "normal style": how she speaks, nothing else
        outcome.update(route="style", reply=await style.choose(app.state.redis, body.user, chosen))
    elif fp and resolved:
        outcome = await _run_fastpath(app, body, fp, resolved)
    else:
        outcome = await _run_llm(app, body, history, request_id)

    latency = int((time.monotonic() - started) * 1000)
    await db.pool().execute(
        "INSERT INTO ask_requests (id, session_id, user_id, text, route, reply, task_ids, latency_ms, screen) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7::uuid[], $8, $9::jsonb)",
        uuid.UUID(request_id), session_id, body.user, body.text, outcome["route"], outcome["reply"],
        [uuid.UUID(t) for t in outcome["task_ids"]], latency,
        body.screen.model_dump_json() if body.screen else None,
    )
    s_cfg = cfg.session
    turn = {"role": "user", "text": body.text, "tasks": outcome["task_ids"]}
    focused = _focused(body.screen)
    if focused:
        turn["about"] = f"{focused.n} · {focused.title or focused.tool}"
    await context.append(app.state.redis, session_id, turn, outcome["reply"], s_cfg.max_turns, s_cfg.ttl_minutes)
    return {"request_id": request_id, "session_id": session_id, "latency_ms": latency, **outcome}


@router.get("/history")
async def history(session_id: str, request: Request):
    """The conversation so far (what the display shows after a reload): what you said, what she said."""
    if not context.valid_session(session_id):
        return {"turns": []}
    turns = await context.load(request.app.state.redis, session_id)
    return {"turns": [{"role": t.get("role"), "text": t.get("text") or "", "about": t.get("about")}
                      for t in turns if t.get("role") in ("user", "assistant")]}


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
    st = await style.current(app.state.redis, body.user)
    if not body.approve:
        return {"reply": style.line(st, "declined", "Okay, I won't."), "task_ids": []}
    try:
        task_id = await launch(app, StartTask(tool=pending["tool"], args=pending["args"], user=body.user,
                                              confirmed=True, device=await auth.display_of(request.headers)))
    except HTTPException as exc:
        return {"reply": style.notice(st, f"I couldn't do that: {_reason(exc)}"), "task_ids": []}
    result = await wait_for_result(task_id, app.state.config.current.llm.wait_for_result_seconds)
    if result and result["status"] == "completed" and pending.get("reply"):
        return {"reply": style.report(st, pending["reply"]), "task_ids": [task_id]}
    if result and result["status"] == "completed":
        return {"reply": style.line(st, "done", "Done."), "task_ids": [task_id]}
    return {"reply": style.notice(st, _summarize(pending["tool"], result)), "task_ids": [task_id]}


# ---- fast path ----------------------------------------------------------------

async def _run_fastpath(app, body: Ask, fp, resolved) -> dict[str, Any]:
    out = await _fastpath_plain(app, body, fp, resolved)
    st = await style.current(app.state.redis, body.user)
    if st:
        if out["needs_confirmation"]:
            out["reply"] = style.confirming(st, out["reply"], plain=out["reply"] == NEEDS_CONFIRMATION)
        elif out.get("_ok"):
            out["reply"] = style.report(st, out["reply"])
        else:
            out["reply"] = style.notice(st, out["reply"])
    out.pop("_ok", None)
    return out


async def _fastpath_plain(app, body: Ask, fp, resolved) -> dict[str, Any]:
    args = fastpath.coerce(fp.args, resolved.tool.input_schema)
    out: dict[str, Any] = {"route": "fastpath", "reply": "", "task_ids": [], "needs_confirmation": []}
    try:
        task_id = await launch(app, StartTask(tool=resolved.qualified_name, args=args, user=body.user,
                                              device=getattr(body, "device", None)))
    except HTTPException as exc:
        if exc.status_code == 409:
            action = await _hold(app, resolved.qualified_name, args, body.user, _reason(exc),
                                 reply=fastpath.render_reply(fp.pattern, fp.groups))
            out["needs_confirmation"].append(action)
            out["reply"] = NEEDS_CONFIRMATION
        else:
            out["reply"] = f"I can't do that: {_reason(exc)}"
        return out
    out["task_ids"].append(task_id)
    result = await wait_for_result(task_id, app.state.config.current.llm.wait_for_result_seconds)
    if result and result["status"] == "failed":
        out["reply"] = f"That didn't work: {result.get('error') or 'the tool failed'}"
    elif (why := await _follow_up(app, body.user, resolved.qualified_name, result, out)):
        out["reply"] = why
    else:
        out["reply"] = fastpath.render_reply(fp.pattern, fp.groups)
        out["_ok"] = True
    return out


async def _follow_up(app, user: str, tool: str, result: dict | None, out: dict) -> str | None:
    """A tool that stopped short of something he has to decide (e.g. a click that would buy or
    send) returns `confirm`: the same function's tool and args to run if he says yes. It's held
    like any change-tier action (and governance still decides when it runs)."""
    if not result or result.get("status") != "completed":
        return None
    data = result.get("result")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    c = data.get("confirm") if isinstance(data, dict) else None
    if not isinstance(c, dict) or not isinstance(c.get("tool"), str):
        return None
    if c["tool"].split(".", 1)[0] != tool.split(".", 1)[0]:        # only its own function's tools
        return None
    reason = str(c.get("reason") or "That needs your confirmation.")[:300]
    action = await _hold(app, c["tool"], dict(c.get("args") or {}), user, reason,
                         reply=str(c["reply"])[:200] if c.get("reply") else None)
    out["needs_confirmation"].append(action)
    return reason


# ---- LLM loop -----------------------------------------------------------------

async def _run_llm(app, body: Ask, history: list[dict[str, Any]], request_id: str) -> dict[str, Any]:
    cfg = app.state.config.current.llm
    out: dict[str, Any] = {"route": "llm", "reply": "", "task_ids": [], "needs_confirmation": []}

    if await _cloud_tokens_this_month() >= cfg.monthly_token_cap and _uses_cloud(cfg):
        out.update(route="error", reply="I've reached this month's model budget, so I can only run "
                                        "fast-path commands until it resets or the cap is raised.")
        return out

    tools, by_api, functions = await _tool_defs(app, body.user)
    if body.screen and body.screen.panels:
        tools = tools + [PANEL_TOOL]
    system = _system_prompt(app, functions, profile.core_prompt(await profile.core(body.user)))
    if (extra := style.prompt(await style.current(app.state.redis, body.user))):
        system += "\n\n" + extra          # last, so the rest of the prompt stays cached
    # What's on screen, what earlier turns found, and what she knows about you that fits
    # this request go with this request only, as data.
    try:
        r = mnemosyne.rules(app)
        known = profile.relevant_lines(await profile.relevant(
            app, body.user, body.text, int(r["recall_facts"]), float(r["recall_min_score"])))
    except Exception:
        log.exception("couldn't recall facts for this request")
        known = []
    ctx = await context.block(body.screen, history, known)
    messages = context.messages(history) + [Message("user", f"{ctx}\n\n{body.text}" if ctx else body.text)]

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
            if call.name == PANEL_TOOL.name:
                result_text, is_error = await _read_panel(body.screen, call.args)
            else:
                result_text, is_error = await _run_tool_call(app, body.user, by_api.get(call.name), call.args, out,
                                                             body.device, body.screen)
            messages.append(Message("tool", result_text, tool_call_id=call.id,
                                    tool_name=call.name, is_error=is_error))

    out["reply"] = "That took more steps than I'm allowed for one request, so I stopped."
    return out


# Reads what's already on your screen, so it isn't a function's tool and needs no task:
# the same data the panel shows, to the same person looking at it.
PANEL_TOOL = ToolDef(
    name="display__panel",
    description="[Display] Read one panel on the user's screen in full, by the number shown on it. Use it when the "
                "short form in the context isn't enough (e.g. to summarize a page, or items past the first 20).",
    input_schema={"type": "object", "properties": {"panel": {"type": "integer", "minimum": 1}}, "required": ["panel"]},
)


def _focused(screen: context.Screen | None) -> context.PanelRef | None:
    if not screen or screen.focus is None:
        return None
    return next((p for p in screen.panels if p.n == screen.focus), None)


async def _read_panel(screen: context.Screen | None, args: dict) -> tuple[str, bool]:
    try:
        n = int(args.get("panel"))
    except (TypeError, ValueError):
        return _data({"status": "error", "error": "say which panel, by its number"}), True
    ref = next((p for p in (screen.panels if screen else []) if p.n == n), None)
    if ref is None:
        shown = sorted(p.n for p in (screen.panels if screen else []))
        return _data({"status": "error", "error": f"there is no panel {n} on screen", "panels": shown}), True
    info = (await context.results([ref.task_id])).get(ref.task_id)
    if info is None:
        return _data({"status": "error", "error": "that panel's data is no longer available"}), True
    return _data({"panel": n, "title": ref.title, "tool": info["tool"], "status": info["status"],
                  "result": info["result"], "error": info["error"]}), False


async def _run_tool_call(app, user: str, qualified: str | None, args: dict, out: dict,
                         device: int | None = None, screen: context.Screen | None = None) -> tuple[str, bool]:
    if qualified is None:
        return _data({"status": "error", "error": "no such tool"}), True
    args = dict(args or {})
    display = "brief" if args.pop(DISPLAY_ARG, None) == "brief" else "panel"
    resolved = await registry.resolve(app.state.redis, qualified)
    if display == "brief" and resolved and resolved.tool.display == "panel":   # always shown: a library, a quiz…
        display = "panel"
    if resolved and _takes_panel(resolved.tool.input_schema):
        try:
            args = with_panel_task(args, screen)
        except ValueError as exc:
            return _data({"status": "error", "error": str(exc)}), True
    try:
        task_id = await launch(app, StartTask(tool=qualified, args=args, user=user, display=display, device=device))
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
    why = await _follow_up(app, user, qualified, result, out)
    if why:
        return _data({"status": "needs_confirmation", "reason": why,
                      "note": "Nothing was pressed. Tell the user what's waiting; they confirm on the display."}), False
    return _data({"status": result["status"], "partial": result["partial"],
                  "result": result["result"], "error": result["error"]}), result["status"] == "failed"


PANEL_TASK = "_panel_task"


def _takes_panel(schema: dict | None) -> bool:
    """A tool that acts on one of his panels (e.g. sending it) marks its `panel` argument."""
    prop = ((schema or {}).get("properties") or {}).get("panel") or {}
    return bool(prop.get("x-panel"))


def with_panel_task(args: dict, screen: context.Screen | None) -> dict:
    """The panel he means, as the task behind it: the number he said, else the one he clicked,
    else the newest on screen. Only this display's panels count, and the model never names tasks."""
    args = {k: v for k, v in args.items() if k != PANEL_TASK}
    panels = [p for p in (screen.panels if screen else [])]
    n = args.get("panel")
    if n not in (None, ""):
        try:
            n = int(n)
        except (TypeError, ValueError):
            raise ValueError("Which panel? Give its number.")
        ref = next((p for p in panels if p.n == n), None)
        if ref is None:
            raise ValueError(f"There's no panel {n} on this screen.")
    else:
        ref = next((p for p in panels if screen and p.n == screen.focus), None)
        if ref is None and not any(args.get(k) for k in ("url", "text")):
            live = [p for p in panels if not p.queued] or panels
            ref = max(live, key=lambda p: p.n) if live else None
    if ref is not None:
        args[PANEL_TASK] = ref.task_id
    return args


MAX_TOOL_OUTPUT = 40_000     # characters handed to the model per tool call; the panel shows everything


def _data(payload: dict) -> str:
    """Tool output is wrapped so the model reads it as data, never as instructions."""
    text = json.dumps(payload, default=str)
    if len(text) > MAX_TOOL_OUTPUT:
        text = text[:MAX_TOOL_OUTPUT] + ' …(cut here; the full result is on the display)"'
    return "<tool_output>\n" + text + "\n</tool_output>"


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
                                input_schema=_plain(tool.input_schema) if tool.display == "panel" else _with_display(tool.input_schema)))
            by_api[api_name(q)] = q
            visible.append(tool.name)
        if visible:
            functions.append({"name": fn.name, "description": fn.description, "tools": visible})
    return sorted(defs, key=lambda d: d.name), by_api, functions


# Every tool call says how much screen its answer deserves. The model decides from the
# question, not the tool: "is the door locked?" is brief, "how's the house?" is a panel.
DISPLAY_ARG = "_display"
DISPLAY_SCHEMA = {"type": "string", "enum": ["panel", "brief"],
                  "description": "How to show the result. 'brief': he asked for one fact, so answer in your reply "
                                 "and open no panel. 'panel' (default): open the result panel; your reply then adds "
                                 "only what matters, never repeats the panel."}


def _plain(schema: dict | None) -> dict:
    """Our own markers (x-…) stay with us; the model gets plain JSON Schema."""
    s = dict(schema or {"type": "object"})
    if s.get("properties"):
        s["properties"] = {k: {kk: vv for kk, vv in v.items() if not kk.startswith("x-")} if isinstance(v, dict) else v
                           for k, v in s["properties"].items()}
    return s


def _with_display(schema: dict) -> dict:
    s = _plain(schema)
    s["properties"] = {**(s.get("properties") or {}), DISPLAY_ARG: DISPLAY_SCHEMA}
    return s


def _system_prompt(app, functions: list[dict], core: str = "") -> str:
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
    # The core profile (Mnemosyne). It changes rarely, so the prompt stays cached.
    if core:
        identity += "\n\n" + core
    return identity


# ---- helpers ------------------------------------------------------------------

async def _hold(app, tool: str, args: dict, user: str, reason: str, reply: str | None = None) -> dict[str, Any]:
    action_id = uuid.uuid4().hex
    await app.state.redis.set(PENDING_PREFIX + action_id,
                              json.dumps({"tool": tool, "args": args, "user": user, "reply": reply}), ex=PENDING_TTL)
    return {"action_id": action_id, "tool": tool, "args": args, "reason": reason}


def _reason(exc: HTTPException) -> str:
    d = exc.detail
    return d.get("reason", str(d)) if isinstance(d, dict) else str(d)


def _summarize(tool: str, result: dict | None) -> str:
    if result is None:
        return "Started; it's still running."
    if result["status"] == "completed":
        return "Done."
    if result.get("error"):
        return f"That didn't work: {result['error']}"
    return f"That ended as {result['status']}."


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
