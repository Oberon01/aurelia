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
import re
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .config import FastPathPattern
from . import auth, context, db, fastpath, mnemosyne, profile, registry, style
from .llm.base import Message, ProviderError, ToolDef
from .settings import settings
from .tasks import StartTask, launch, wait_for_result

router = APIRouter(prefix="/ask", tags=["assistant"])
log = logging.getLogger("aurelia.assistant")

PENDING_PREFIX = "aurelia:pending:"
PENDING_TTL = 600
NEEDS_CONFIRMATION = "That needs your confirmation first."
CLOUD_PROVIDERS = ("claude",)          # what llm_calls.provider says for any Claude model


def is_cloud(name: str) -> bool:
    return name == "claude" or name.startswith("claude_")


def needs_reasoning(text: str, cfg) -> bool:
    """A question that deserves the stronger model (llm.routes.reason), if one is set."""
    import re
    if not cfg.routes.get("reason"):
        return False
    t = (text or "").lower()
    for pattern in cfg.reason_when:
        try:
            if re.search(pattern, t):
                return True
        except re.error:
            continue
    return False


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
    from . import checkctx
    chk = checkctx.current()                                    # one of Swallowtail's field checks
    body.device = checkctx.DEVICE if chk is not None else await auth.display_of(request.headers)
    # First thing you ask after the digest hour brings up the morning digest (once a day), right here.
    from . import digest
    if chk is None:
        asyncio.create_task(digest.maybe_show(app, body.user, body.device))
    request_id = str(uuid.uuid4())
    # The display makes its own session id, so two requests sent at once share one conversation.
    session_id = body.session_id if context.valid_session(body.session_id) else uuid.uuid4().hex
    history = await context.load(app.state.redis, session_id)

    outcome: dict[str, Any] = {"route": "llm", "reply": "", "task_ids": [], "needs_confirmation": []}

    chosen = style.switch(body.text) if chk is None else None     # a check never changes how she speaks
    fp = fastpath.match(body.text, cfg.fastpath.patterns) if cfg.fastpath.enabled and chosen is None else None
    from . import when as _when
    repeats = _when.mentions_repeat(body.text) or _CONDITIONAL.search(body.text) is not None
    if repeats and fp and not fp.pattern.tool.startswith("pulsar."):
        fp = None                           # "lock the door every night at 11", "flip the porch light when the door
                                            # unlocks if it's dark": an automation (or the model asks), never a command now
    later = bool(fp and fp.pattern.tool in ("admiral.schedule", "pulsar.automate", "pulsar.routine"))
    if later:
        from . import engine, pulsar        # "tomorrow I have a meeting" isn't a command for later: the model hears it
        say_ = fp.args.get("say") or ""
        ok = (engine.fast_ok(app, say_) if fp.pattern.tool == "admiral.schedule" else
              await pulsar.fast_ok(app, say_) if fp.pattern.tool == "pulsar.automate" else
              await pulsar.routine_fast_ok(app, say_))
        if not ok:
            fp = None
    if chosen is None and cfg.fastpath.enabled and not later and not repeats and not _timed(app, body.text):
        from . import morpho                # his skills' phrases (Morpho): a skill runs like a fast path
        try:
            hit = await morpho.match_phrase(app, body.text)
        except Exception:
            log.exception("skill phrases")
            hit = None
        if hit is not None:
            sk, slots = hit
            fp = fastpath.FastPathMatch(
                pattern=FastPathPattern(name=f"skill-{sk['id']}", match="", tool="morpho.run", args={},
                                        reply=f"Running your skill: {sk['name']}."),
                args={"skill": str(sk["id"]), "slots": slots}, groups={})
    if fp is None and chosen is None and cfg.fastpath.enabled and not later and not repeats:
        hit = await _connection_tool(app, body.text)          # "gitea list repos": a connected service's own tool
        if hit is not None:
            q, label = hit
            fp = fastpath.FastPathMatch(pattern=FastPathPattern(name=f"connection-{q}", match="", tool=q, args={},
                                                                reply=f"Asking {label}."), args={}, groups={})
    resolved = await registry.resolve(app.state.redis, fp.pattern.tool) if fp else None
    elsewhere = None
    if chosen is None and cfg.fastpath.enabled and not later and not repeats and not (fp and fp.pattern.name.startswith("skill-")):
        elsewhere = await _lookup_elsewhere(app, body.text)   # "send the device list to my phone"
    if chosen is not None:              # "use Raphael style" / "normal style": how she speaks, nothing else
        outcome.update(route="style", reply=await style.choose(app.state.redis, body.user, chosen))
    elif elsewhere:
        outcome = await _run_elsewhere(app, body, *elsewhere)
    elif fp and resolved:
        outcome = await _run_fastpath(app, body, fp, resolved)
    elif chk is not None and not chk.get("model"):              # a check doesn't call the model unless it says so
        outcome.update(route="llm", reply="", model_called=False)
    else:
        outcome = await _run_llm(app, body, history, request_id)

    if (outcome["route"] == "llm" and not outcome.pop("_panel_opened", False) and long_reply(outcome["reply"], cfg.display)
            and chk is None):
        try:                                       # too much for the top line: it opens as a panel to read
            outcome["reply_panel"] = await _reply_panel(app, body, outcome["reply"])
            outcome["reply_short"] = first_sentence(outcome["reply"])
        except Exception:
            log.exception("couldn't open the reply as a panel")
    outcome.pop("_panel_opened", None)

    latency = int((time.monotonic() - started) * 1000)
    if chk is not None:                # a check: not one of his requests (never in the record or his habits)
        return {"request_id": request_id, "session_id": session_id, "latency_ms": latency, **outcome}
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
    from . import morpho
    await morpho.record(app, pending["tool"], pending["args"], body.user, bool(body.approve), "reply")
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
    said = (result or {}).get("result") if result and result["status"] == "completed" else None
    if isinstance(said, dict) and isinstance(said.get("did"), dict) and said["did"].get("what"):
        return {"reply": style.report(st, str(said["did"]["what"])[:300]), "task_ids": [task_id]}   # what it did, as it did it
    if result and result["status"] == "completed":
        return {"reply": style.line(st, "done", "Done."), "task_ids": [task_id]}
    return {"reply": style.notice(st, _summarize(pending["tool"], result)), "task_ids": [task_id]}


# ---- fast path ----------------------------------------------------------------

import re as _re_send
# "… when the door unlocks", "whenever I get home, …": a condition, so what it says is never done right now.
# (Questions aren't conditions: "when is …", "when does …".)
_CONDITIONAL = _re_send.compile(
    r"\b(?:when(?:ever)?|each\s+time|every\s+time|any\s+time|as\s+soon\s+as)\s+"
    r"(?!(?:is|are|was|were|do|does|did|will|would|can|could|should|has|have|had)\b)\w"
    # "… if the door opens", "… once I leave", "… until I get home", "… after I leave" (not "check if …", a question)
    r"|(?<!check\s)(?<!see\s)(?<!ask\s)(?<!know\s)(?<!out\s)(?<!me\s)(?<!wonder\s)"
    r"\b(?:once|until|till|while|after|before|next\s+time)\s+"
    r"(?:i|i'm|im|i've|you|you're|the|my|it|it's|its|someone|somebody|anyone|everyone|everybody|no\s+one|nobody|"
    r"he|she|they|we|there|there's)\b"
    # "… if it's dark", "… if Sarah leaves", "… unless …", "… in case …": any "if" that isn't a question
    r"|(?<!check\s)(?<!see\s)(?<!ask\s)(?<!know\s)(?<!out\s)(?<!me\s)(?<!wonder\s)(?<!asks\s)(?<!asking\s)"
    r"(?<!checking\s)(?<!whether\s)\b(?:if|unless|in\s+case)\s+\w"
    r"|\b(?:after|at|before|around|by)\s+(?:sunset|sunrise|dusk|dawn|dark)\b"
    r"|\b(?:during|in)\s+(?:the\s+)?quiet\s+hours\b", _re_send.I)
SEND_ELSEWHERE = _re_send.compile(
    r"^send\s+(?!(?:panel\s+\d+|this|that|it|this\s+panel|that\s+panel|a\s+note|note|https?://)\b)"
    r"(?P<what>.+?)\s+(?:to|over\s+to|onto|on)\s+(?:the\s+|my\s+)?(?P<to>[\w' -]+?)$", _re_send.I)
NOT_ELSEWHERE = ("constellation.look", "constellation.watch", "constellation.screens", "constellation.send")


async def _lookup_elsewhere(app, text: str) -> tuple[str, dict, str, str] | None:
    """A look-up he wants on another display, by fast path: "send the device list to my phone". The part before
    "to" must be a look-up I know by its own fast path (as said, or as "show …"); else it isn't one."""
    t = " ".join(text.strip().rstrip(".!?").split())
    t = fastpath._POLITE.match(t).group("rest") or t
    m = SEND_ELSEWHERE.match(t)
    if not m:
        return None
    what, to = m.group("what").strip(), m.group("to").strip()
    pats = [p for p in app.state.config.current.fastpath.patterns
            if not p.name.startswith(("schedule-", "send-")) and p.tool not in NOT_ELSEWHERE]
    plural = _re_send.sub(r"\b(\w+?)\s+list\b", r"\1s", what)
    for cand in dict.fromkeys([what, plural, f"show {what}", f"show {plural}", f"show me {what}", f"what's {what}",
                               f"list {plural}"]):
        hit = fastpath.match(cand, pats)
        if hit is None:
            continue
        fn, _, tool = hit.pattern.tool.partition(".")
        if app.state.governance.decide(fn, tool, "owner", False).tier not in ("reference", "read"):
            return None                                 # an action ("put frieren on the tv"): not a look-up to send
        resolved = await registry.resolve(app.state.redis, hit.pattern.tool)
        if resolved is None:
            return None
        return hit.pattern.tool, fastpath.coerce(hit.args, resolved.tool.input_schema), to, what
    return None


async def _run_elsewhere(app, body, qualified: str, args: dict, to: str, what: str) -> dict[str, Any]:
    out: dict[str, Any] = {"route": "fastpath", "reply": "", "task_ids": [], "needs_confirmation": []}
    from . import checkctx
    chk = checkctx.current()
    if chk is not None and chk["mode"] == "auto":       # a check on its own never shows anything on his displays
        chk["blocked"].append("constellation.send")
        out["reply"] = "That would show something on another display, so this check waits for your Start."
        return out
    text, err = await _run_for_display(app, body.user, qualified, args, to, getattr(body, "device", None), out)
    try:
        r = json.loads(text)
    except ValueError:
        r = {}
    if err:
        out["reply"] = f"I couldn't: {r.get('error') or r.get('reason') or 'it failed'}"
    else:
        out["reply"] = r.get("note") or f"Sent {what} to {to}."
        out["_ok"] = True
    return out

def _timed(app, text: str) -> bool:
    """A request with a time in it ("… tomorrow at 9", "… every morning") is never a skill's phrase now."""
    import re as _re
    from . import engine, when
    if _re.search(r"\b(?:every|each)\s+(?:\d+\s+)?(?:day|morning|night|evening|week|weekday|weekend|hour|minute|month|"
                  r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b", text, _re.I):
        return True
    if _re.search(r"\b(?:tomorrow|tonight|today|later|soon|(?:on\s+|next\s+|this\s+)?(?:mon|tues|wednes|thurs|fri|satur|"
                  r"sun)days?|(?:this|tomorrow)\s+(?:morning|afternoon|evening|night))\s*[.!?]*$", text.strip(), _re.I):
        return True
    try:
        when.split(text, engine.now(), engine.tz_name(app))
        return True
    except Exception:                      # not a time (or nonsense like "in 99999999999999 days"): no time in it
        return False

async def _run_fastpath(app, body: Ask, fp, resolved) -> dict[str, Any]:
    from . import checkctx
    out = await _fastpath_plain(app, body, fp, resolved)
    st = await style.current(app.state.redis, body.user) if checkctx.current() is None else None   # checks: plain words
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
    if _takes_panel(resolved.tool.input_schema):    # "send panel 2" / "send it": the same panel the model would mean
        try:
            args = with_panel_task(args, getattr(body, "screen", None))
        except ValueError as exc:
            out["reply"] = str(exc)
            return out
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
    if result is None:
        asyncio.create_task(_late_follow_up(app, body.user, resolved.qualified_name, task_id,
                                            getattr(body, "device", None)))
    if result and result["status"] == "failed":
        out["reply"] = f"That didn't work: {result.get('error') or 'the tool failed'}"
    elif (why := await _follow_up(app, body.user, resolved.qualified_name, result, out)):
        out["reply"] = why
    else:
        out["reply"] = fastpath.render_reply(fp.pattern, fp.groups)
        out["_ok"] = True
    return out


async def _late_follow_up(app, user: str, tool: str, task_id: str, device: int | None, wait: float = 600) -> None:
    """A tool that was still running when she replied may still end in a confirmation (installing,
    removing, closing a program): when it does, the card goes to the display that asked."""
    from .events import PanelEvent, publish
    try:
        result = await wait_for_result(task_id, wait)
        held: dict = {"needs_confirmation": []}
        if await _follow_up(app, user, tool, result, held):
            for action in held["needs_confirmation"]:
                await publish(app.state.redis, PanelEvent(type="confirm.ask", task_id=task_id, tool=action["tool"],
                                                          device=device, data={"action": action}))
    except Exception:
        log.exception("couldn't bring a late confirmation for %s", tool)


# A function may hand him a confirmation for another function's tool only where it's named here.
HANDOFFS = {"quasar": ("constellation.install_package",)}     # "the TV PC needs my player: install it?"


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
    if c["tool"].split(".", 1)[0] != tool.split(".", 1)[0] and c["tool"] not in HANDOFFS.get(tool.split(".", 1)[0], ()):
        return None                                                 # only its own function's tools (or a named handoff)
    # A read-back he confirms (an automation, a routine's steps) is shown whole; it's kept short where it's made.
    reason = str(c.get("reason") or "That needs your confirmation.")[:900 if c["tool"] == "pulsar.save" else 300]
    action = await _hold(app, c["tool"], dict(c.get("args") or {}), user, reason,
                         reply=str(c["reply"])[:200] if c.get("reply") else None)
    out["needs_confirmation"].append(action)
    return reason


# ---- LLM loop -----------------------------------------------------------------

# ---- connected services (Lagrange) -------------------------------------------------------------------------

_LEADS = re.compile(r"^(?:please\s+)?(?:ask|use|on|in|with|tell)\s+", re.I)
_FILLER = {"my", "the", "all", "a", "an", "me", "please", "to", "for", "of", "show", "get"}


async def _named_connection(app, text: str):
    """A request that starts with a connected service's name ("gitea, list my repos", "ask home assistant to …"):
    (its registration, the rest of the request), or None."""
    t = _LEADS.sub("", " ".join(text.strip().split()))
    for fn in await registry.list_all(app.state.redis):
        if fn.via != "lagrange":
            continue
        name = fn.name.replace("_", " ")
        m = re.match(rf"^(?:the\s+|my\s+)?{re.escape(name)}(?:'s)?\b[\s,:]+(?:to\s+)?(?P<rest>.+)$", t, re.I)
        if m:
            return fn, m.group("rest").strip(" ?.!")
    return None


async def _connection_tool(app, text: str) -> tuple[str, str] | None:
    """"gitea list repos" -> 'gitea.list_repos' when the words are exactly a tool's name and it needs nothing
    else. Anything more goes to the model, which then sees only that service's tools."""
    hit = await _named_connection(app, text)
    if hit is None:
        return None
    fn, rest = hit
    said = {w for w in re.findall(r"[a-z0-9]+", rest.lower())} - _FILLER
    for tool in fn.tools:
        words = set(tool.name.split("_")) - _FILLER
        if said and said == words and not (tool.input_schema or {}).get("required"):
            return f"{fn.name}.{tool.name}", fn.name.replace("_", " ").title()
    return None


async def _run_llm(app, body: Ask, history: list[dict[str, Any]], request_id: str) -> dict[str, Any]:
    cfg = app.state.config.current.llm
    out: dict[str, Any] = {"route": "llm", "reply": "", "task_ids": [], "needs_confirmation": []}

    if await _cloud_tokens_this_month() >= cfg.monthly_token_cap and _uses_cloud(cfg):
        out.update(route="error", reply="I've reached this month's model budget, so I can only run "
                                        "fast-path commands until it resets or the cap is raised.")
        return out

    named = await _named_connection(app, body.text)
    tools, by_api, functions = await _tool_defs(app, body.user, only=named[0].name if named else None)
    if body.screen and body.screen.panels:
        tools = tools + [PANEL_TOOL]
    from . import history as my_history
    try:
        about_me = await my_history.self_context(app)    # where I am on the roadmap, my latest releases
    except Exception:
        log.exception("couldn't read my own history for the prompt")
        about_me = ""
    system = _system_prompt(app, functions, profile.core_prompt(await profile.core(body.user)), about_me)
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

    purpose = "reason" if needs_reasoning(body.text, cfg) else "chat"
    out["model_route"] = purpose
    for _ in range(cfg.max_tool_rounds + 1):
        started = time.monotonic()
        try:
            completion = await app.state.llm.complete(purpose, system, messages, tools)
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
    on = str(args.pop(ON_ARG, "") or "").strip()
    if on:
        return await _run_for_display(app, user, qualified, args, on, device, out)
    try:
        task_id = await launch(app, StartTask(tool=qualified, args=args, user=user, display=display, device=device))
        if display == "panel" and qualified not in QUIET_TOOLS:
            out["_panel_opened"] = True                # the turn already has a panel: the reply stays short
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
        asyncio.create_task(_late_follow_up(app, user, qualified, task_id, device))
        return _data({"status": "running", "task_id": task_id,
                      "note": "Still running; progress is shown on the display. It isn't done: don't say it "
                              "happened. If it needs his OK, a confirmation card appears when it's ready."}), False
    why = await _follow_up(app, user, qualified, result, out)
    if why:
        return _data({"status": "needs_confirmation", "reason": why,
                      "note": "Nothing was pressed. Tell the user what's waiting; they confirm on the display."}), False
    return _data({"status": result["status"], "partial": result["partial"],
                  "result": result["result"], "error": result["error"]}), result["status"] == "failed"


# ---- a look-up shown on another display ("send the device list to my iPhone") -----------------

ELSEWHERE = 0          # a task whose panel is for other displays: its own events reach no display


async def _run_for_display(app, user: str, qualified: str, args: dict, on: str, asker: int | None,
                           out: dict) -> tuple[str, bool]:
    """Runs a look-up once, without a panel here, and puts its result on the display(s) he named,
    the same way send does: big on a presentation display, kept for one that isn't open."""
    from . import constellation
    fn, _, tool = qualified.partition(".")
    if qualified in NOT_ELSEWHERE:                    # a screen's contents, or another send: never to another display
        return _data({"status": "refused", "reason": f"{qualified} can't be shown on another display."}), True
    tier = app.state.governance.decide(fn, tool, user, False).tier
    if tier not in ("reference", "read"):
        return _data({"status": "refused", "reason": "Only look-ups can be shown on another display; "
                                                     "do this one here, or send its panel afterwards."}), True
    try:
        targets, label = await constellation.send_targets(app, on, asker)
    except ValueError as exc:
        return _data({"status": "error", "error": str(exc)}), True
    try:
        task_id = await launch(app, StartTask(tool=qualified, args=args, user=user, device=ELSEWHERE, trigger="send"))
    except HTTPException as exc:
        return _data({"status": "refused", "reason": _reason(exc)}), True
    out["task_ids"].append(task_id)
    out["_panel_opened"] = True                        # her reply stays a sentence; the panel is over there
    me = await db.pool().fetchval("SELECT name FROM devices WHERE id=$1", asker) if asker else None

    async def deliver(result: dict) -> dict:
        return await constellation.deliver(app, user, targets, qualified, args, result, me or "another display",
                                           sender=asker)

    result = await wait_for_result(task_id, app.state.config.current.llm.wait_for_result_seconds)
    if result is None:                                  # a slow look-up: it goes over when it's done
        async def later():
            r = await wait_for_result(task_id, 600)
            if r and r["status"] == "completed" and r["result"]:
                await deliver(r["result"])
        asyncio.create_task(later())
        return _data({"status": "running", "note": f"Still running; it will appear on {label} when it's done."}), False
    if result["status"] != "completed" or not result["result"]:
        return _data({"status": result["status"], "error": result["error"] or "nothing to show"}), True
    sent = await deliver(result["result"])
    note = " ".join(x for x in (f"Confirmed on {', '.join(sent['shown'])}." if sent["shown"] else None,
                                (constellation.delivery_note(sent) or "") + ("." if sent["later"] else "")) if x)
    return _data({"status": "sent", "confirmed_on": sent["shown"], "kept_for": sent["later"], "note": note,
                  "result": result["result"]}), False


# ---- long replies open as a panel --------------------------------------------------------------

REPLY_TOOL = "aurelia.reply"
QUIET_TOOLS = {"constellation.send", "constellation.show"}
NOT_FOR_MODEL = {"pulsar.save", "lagrange.connect", "lagrange.approve", "lagrange.turn_on",
                 "constellation.show"}       # routines' and automations' steps; the model uses _on   # his own taps and words only               # only his confirmation of a read-back runs it           # tools whose panel isn't something to read
_SENTENCE = __import__("re").compile(r"(?<=[.!?])[\"')\]]*\s+(?=[A-Z0-9\"'(])")


_ABBREV = ("Dr.", "Mr.", "Mrs.", "Ms.", "St.", "vs.", "e.g.", "i.e.", "etc.", "approx.", "No.")


def sentences(text: str) -> list[str]:
    out: list[str] = []
    for part in _SENTENCE.split(" ".join((text or "").split())):
        if out and out[-1].endswith(_ABBREV):
            out[-1] += " " + part                    # "Dr. Smith" is one sentence
        elif part.strip():
            out.append(part)
    return out


def long_reply(text: str, display_cfg) -> bool:
    text = (text or "").strip()
    return bool(text) and (len(sentences(text)) >= display_cfg.reply_panel_sentences
                           or len(text) > display_cfg.reply_panel_chars or text.count("\n") >= 2)


def first_sentence(text: str, limit: int = 160) -> str:
    one = (sentences(text) or [text.strip()])[0]
    return one if len(one) <= limit else one[:limit - 1].rsplit(" ", 1)[0] + "…"


async def _reply_panel(app, body: Ask, text: str) -> str:
    """Her whole answer as a panel on the display that asked: there to read, pin, or send on."""
    from .events import PanelEvent, publish
    task_id = str(uuid.uuid4())
    args = {"asked": body.text[:200]}
    data = {"headline": first_sentence(text), "answer": text, "asked": body.text[:200]}
    await db.pool().execute(
        "INSERT INTO task_runs (id, tool, args, trigger, user_id, tier, confirmed, device, status, result, "
        "finished_at, duration_ms) VALUES ($1, $2, $3::jsonb, 'reply', $4, 'reference', false, $5, 'completed', "
        "$6::jsonb, now(), 0)", uuid.UUID(task_id), REPLY_TOOL, json.dumps(args), body.user, body.device, json.dumps(data))
    redis = app.state.redis
    await publish(redis, PanelEvent(type="panel.open", task_id=task_id, tool=REPLY_TOOL, device=body.device, data={
        "args": args, "user": body.user, "trigger": "reply", "tier": "said", "cancellable": False, "key": task_id,
        "subtitle": "› " + body.text[:120]}))
    await publish(redis, PanelEvent(type="panel.result", task_id=task_id, tool=REPLY_TOOL, device=body.device,
                                    data={"status": "completed", "partial": False, **data}))
    await publish(redis, PanelEvent(type="panel.close", task_id=task_id, tool=REPLY_TOOL, device=body.device,
                                    data={"status": "completed", "has_result": True, "error": None}))
    return task_id


PANEL_TASK = "_panel_task"


def _takes_panel(schema: dict | None) -> bool:
    """A tool that acts on one of his panels (e.g. sending it) marks its `panel` argument."""
    prop = ((schema or {}).get("properties") or {}).get("panel") or {}
    return bool(prop.get("x-panel"))


def _sendable(task_id: str) -> bool:
    """A panel backed by a task (a result that can go elsewhere), not a live one (now playing, a heads-up)."""
    try:
        uuid.UUID(str(task_id))
        return True
    except ValueError:
        return False


def with_panel_task(args: dict, screen: context.Screen | None) -> dict:
    """The panel he means, as the task behind it: the number he said, else the one he clicked,
    else the newest on screen. Only this display's panels count, and the model never names tasks.
    Live panels (now playing, a heads-up) aren't results, so "it" skips them and naming one says why."""
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
        if not _sendable(ref.task_id):
            raise ValueError(f"Panel {n} ({ref.title or 'a live one'}) updates as things happen, so it can't be sent. "
                             "Ask for it on the other display instead.")
    else:
        ref = next((p for p in panels if screen and p.n == screen.focus and _sendable(p.task_id)), None)
        if ref is None and not any(args.get(k) for k in ("url", "text")):
            ok = [p for p in panels if _sendable(p.task_id)]
            live = [p for p in ok if not p.queued] or ok
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


async def _tool_defs(app, user: str, only: str | None = None) -> tuple[list[ToolDef], dict[str, str], list[dict]]:
    """Tools the model may see: registered, enabled by policy, not blocked for this user.
    Also returns the functions those tools belong to, for the system prompt. `only`: a request that names a
    connected service sees that service's tools and nothing else."""
    gov = app.state.governance
    defs, by_api, functions = [], {}, []
    for fn in await registry.list_all(app.state.redis):
        if only is not None and fn.name != only:
            continue
        visible = []
        for tool in fn.tools:
            decision = gov.decide(fn.name, tool.name, user, confirmed=True)
            if not decision.allowed:
                continue
            q = f"{fn.name}.{tool.name}"
            if q in NOT_FOR_MODEL:
                continue
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


ON_ARG = "_on"
ON_SCHEMA = {"type": "string",
             "description": "Show the result on another of his displays instead of this one, without opening it "
                            "here: a display's name ('my iphone', 'the TV') or a group ('all presentation devices', "
                            "'everywhere'). Only for look-ups. Leave out to show it here."}


def _plain(schema: dict | None) -> dict:
    """Our own markers (x-…) stay with us; the model gets plain JSON Schema (plus where to show it)."""
    s = dict(schema or {"type": "object"})
    props = {k: {kk: vv for kk, vv in v.items() if not kk.startswith("x-")} if isinstance(v, dict) else v
             for k, v in (s.get("properties") or {}).items()}
    s["properties"] = {**props, ON_ARG: ON_SCHEMA}
    return s


def _with_display(schema: dict) -> dict:
    s = _plain(schema)
    s["properties"] = {**(s.get("properties") or {}), DISPLAY_ARG: DISPLAY_SCHEMA}
    return s


def _system_prompt(app, functions: list[dict], core: str = "", about_me: str = "") -> str:
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
    if about_me:
        identity += "\n\n" + about_me
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
    return any(is_cloud(p) for route in ("chat", "reason") for p in cfg.routes.get(route, [cfg.default_provider]))


async def _cloud_tokens_this_month() -> int:
    """Cloud tokens since midnight on the 1st in his time zone (the cap's month, as the budget panel shows it)."""
    from datetime import datetime, timezone
    from . import budget
    start, _ = budget.month_bounds(datetime.now(timezone.utc), budget.tz_name())
    return await db.pool().fetchval(
        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM llm_calls "
        "WHERE provider = ANY($1::text[]) AND at >= $2", list(CLOUD_PROVIDERS), start) or 0


async def _log_llm_call(request_id: str, completion, started: float) -> None:
    await db.pool().execute(
        "INSERT INTO llm_calls (request_id, provider, model, input_tokens, output_tokens, cache_read_tokens, latency_ms) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        uuid.UUID(request_id), completion.provider, completion.model, completion.usage.input_tokens,
        completion.usage.output_tokens, completion.usage.cache_read_tokens,
        int((time.monotonic() - started) * 1000),
    )


class _CheckRequest:
    """What ask() needs from a request, for a field check made inside the stack (no display, no headers)."""
    def __init__(self, app):
        self.app = app
        self.headers: dict[str, str] = {}


async def ask_for_check(app, text: str, session_id: str) -> dict[str, Any]:
    """One of Swallowtail's checks says something, exactly as he would (checkctx must be set by the caller)."""
    return await ask(Ask(text=text, session_id=session_id), _CheckRequest(app))
