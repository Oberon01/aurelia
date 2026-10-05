"""Swallowtail: field checks of each release, in his home (part of Aurelia's core, after Pulsar).

Chrysalis's tests run in a container before a release deploys. These run afterwards, on the live system, the way
he'd use it: each release that adds or changes a file in config/checks/ has those checks run once it's live.

  config/checks/<name>.yaml   one release's checks (a phase's, a feature's): its title and a list of checks
  config/checks/_home.yaml    his home's names the checks use ({light}, {lock}, {computer}…); a check that needs
                              one he hasn't set is skipped and says which

A check is the input (something he'd say, or a tool and its arguments), what's expected (the route, the tool it
runs, the reply, the result, a confirmation asked for), and how I find out what actually happened: the result
itself, or a device's own reported state afterwards ("verify": a look-up polled until it matches, or one of his
automations' runs). Nothing counts because a command was sent.

How they run (checkctx has the rules every request in a check follows):
  - On their own, right after the release: only checks that look. A check is never confirmed (an action that
    asks for his OK is recorded as asked, then declined), never uses earned trust, and doesn't call the model.
    Anything that would act is refused and waits for his Start.
  - After his Start (never during quiet hours): the checks that act, in order. A check may give him something
    to do first ("say … and confirm it"), wait for what it should cause, ask him a yes/no question only he can
    answer, and clean up after itself ("after").
  - The report: one panel, one row per check (passed, failed, waiting for you, skipped), each with the input
    given, what was expected and what actually happened. Kept on the release, so "how did the last release's
    checks go" works later. Failures are a notice (Pulsar's, his severity), with a rollback offered, never done.
"""
import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError

from . import auth, checkctx, db, registry

log = logging.getLogger("aurelia.swallowtail")
router = APIRouter(prefix="/swallowtail", tags=["swallowtail"])
NAME = "swallowtail"
REPORT_TOOL = f"{NAME}.report"
HOME_FILE = "_home.yaml"
ANSWER_WAIT = 15 * 60                      # how long a question waits for his answer
RELEASE_DELAY = 30                         # seconds after a release goes live (functions re-register first)
STALE = 180                                # a run whose process stopped updating it (a restart) is over
_live: set[asyncio.Task] = set()


# ---- the checks files -----------------------------------------------------------------------------------

class Verify(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str | None = None                # a look-up that reports the device's state (e.g. orbit.status)
    args: dict[str, Any] = {}
    find: dict[str, Any] = {}              # {"list": "devices", "name": "{light}"}: which item of the result
    field: str | None = None
    equals: Any = None
    automation: str | None = None          # or: words in one of his automations' titles…
    outcome: str = "done"                  # …and how its run since the check started must have ended
    within: str | int = "30s"


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    say: str | None = None                 # what he'd say (through the fast path, or the model if model: true)
    tool: str | None = None                # or a tool, called directly
    args: dict[str, Any] = {}
    model: bool = False                    # let the model answer (it may act, so it waits for his Start)
    acts: bool = False                     # does something in his home: waits for his Start
    needs_you: str | None = None           # something he does first ("say … and confirm the read-back")
    question: str | None = None            # a yes/no only he can answer ("Did the heads-up show first?")
    expect: dict[str, Any] = {}
    verify: Verify | None = None
    after: list[dict[str, Any]] = []       # clean up: {"tool": …, "args": …} or {"say": …} (after his Start)
    within: str | int = "20s"              # how long its task may take


class CheckFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    release: str
    phase: str | int | None = None
    checks: list[Check]


EXPECT_KEYS = {"route", "tool", "asks_to_confirm", "status", "reply_starts", "reply_has", "reply_not_has",
               "reply_matches", "result", "result_has", "error_has", "confirmed"}
ROUTES = {"fastpath", "llm", "not fastpath"}


def checks_dir(app) -> Path:
    from .settings import settings
    return Path(settings.config_path).parent / "checks"


def _seconds(v: str | int | float) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hours?)?\s*", str(v))
    if not m:
        raise ValueError(f"\"{v}\" isn't a length of time (like 30s or 5m)")
    n, u = float(m.group(1)), (m.group(2) or "s")[0]
    return n * {"s": 1, "m": 60, "h": 3600}[u]


def needs(c: Check) -> str:
    """'auto' (only looks: runs on its own) or 'start' (acts, needs him, or calls the model: waits for his Start)."""
    if c.acts or c.model or c.needs_you or c.question or c.after or (c.verify and c.verify.automation):
        return "start"
    return "auto"


def validate(f: CheckFile) -> list[str]:
    """What's wrong with a file, in plain words (an empty list: nothing)."""
    out, seen = [], set()
    for c in f.checks:
        where = f"check \"{c.id}\""
        if c.id in seen:
            out.append(f"{where} appears twice")
        seen.add(c.id)
        if c.say and c.tool:
            out.append(f"{where}: say or tool, not both")
        if not (c.say or c.tool or c.needs_you or c.question or c.verify):
            out.append(f"{where} has nothing to do")
        bad = set(c.expect) - EXPECT_KEYS
        if bad:
            out.append(f"{where}: unknown expect {', '.join(sorted(bad))}")
        if "route" in c.expect and c.expect["route"] not in ROUTES:
            out.append(f"{where}: route is one of {', '.join(sorted(ROUTES))}")
        if c.expect and not (c.say or c.tool):
            out.append(f"{where}: expect needs a say or a tool")
        if not (c.expect or c.verify or c.question):
            out.append(f"{where} checks nothing (no expect, verify or question)")
        for st in c.after:
            if not (isinstance(st.get("say"), str) or isinstance(st.get("tool"), str) or st.get("delete") == "automation"):
                out.append(f"{where}: each after step is a say, a tool, or delete: automation")
        if c.verify and not (c.verify.automation or (c.verify.tool and c.verify.field)):
            out.append(f"{where}: verify needs a tool and a field, or an automation")
        for t in (c.within, *( [c.verify.within] if c.verify else [])):
            try:
                _seconds(t)
            except ValueError as exc:
                out.append(f"{where}: {exc}")
    return out


def load_file(path: Path) -> tuple[CheckFile | None, str | None]:
    try:
        raw = yaml.safe_load(path.read_text()) or {}
        f = CheckFile.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        return None, f"{path.name} can't be read: {str(exc).splitlines()[0][:300]}"
    problems = validate(f)
    if problems:
        return None, f"{path.name}: " + "; ".join(problems)[:600]
    return f, None


def files(app) -> list[Path]:
    d = checks_dir(app)
    return sorted(p for p in d.glob("*.yaml") if p.name != HOME_FILE) if d.is_dir() else []


def home_vars(app) -> dict[str, str]:
    p = checks_dir(app) / HOME_FILE
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    got = raw.get("vars") if isinstance(raw, dict) else None
    return {str(k): str(v) for k, v in got.items() if v not in (None, "")} if isinstance(got, dict) else {}


# ---- {names}: his home's names, and times relative to now -------------------------------------------------

VAR = re.compile(r"\{([a-z][a-z0-9_]*)\}")


class Missing(Exception):
    pass


def _builtin(app, name: str) -> str | None:
    m = re.fullmatch(r"in_(\d{1,3})_minutes?", name)
    if m:                                           # "{in_3_minutes}" -> "7:42 pm" in his time zone
        from . import engine
        at = datetime.now(ZoneInfo(engine.tz_name(app))) + timedelta(minutes=int(m.group(1)) + 1)
        return f"{(at.hour % 12) or 12}:{at.minute:02d} {'am' if at.hour < 12 else 'pm'}"
    return None


def fill(app, value: Any, names: dict[str, str]) -> Any:
    """Put his names into a check (strings anywhere inside it). Missing: a name he hasn't set."""
    if isinstance(value, str):
        def one(m):
            k = m.group(1)
            if k in names:
                return names[k]
            b = _builtin(app, k)
            if b is not None:
                names[k] = b                        # the same time everywhere in one check
                return b
            raise Missing(k)
        return VAR.sub(one, value)
    if isinstance(value, dict):
        return {k: fill(app, v, names) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(app, v, names) for v in value]
    return value


# ---- running one check ------------------------------------------------------------------------------------

def _trim(v: Any, room: int = 1500) -> Any:
    s = json.dumps(v, default=str)
    if len(s) <= room:
        return v
    return s[:room] + "…"


def _path(data: Any, path: str) -> Any:
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _same(a: Any, b: Any) -> bool:
    if isinstance(b, bool) or isinstance(a, bool):
        return a is b or (isinstance(a, str) and a.lower() == str(b).lower())
    if isinstance(b, (int, float)) and isinstance(a, (int, float)):
        return float(a) == float(b)
    return str(a).strip().lower() == str(b).strip().lower()


def _line(what: str, expected: Any, actual: Any, ok: bool) -> dict:
    show = lambda v: v if isinstance(v, str) else json.dumps(v, default=str)
    return {"what": what, "expected": show(expected)[:300], "actual": show(actual)[:300], "ok": bool(ok)}


def judge(expect: dict, act: dict) -> list[dict]:
    """Each expectation against what actually happened: one line each."""
    lines = []
    tools = [t["tool"] for t in act.get("tools", [])]
    first = (act.get("tools") or [{}])[0]
    reply = str(act.get("reply") or "")
    for k, v in expect.items():
        if k == "route":
            r = act.get("route")
            ok = (r != "fastpath") if v == "not fastpath" else (r == v)
            lines.append(_line("route", v, r, ok))
        elif k == "tool":
            ok = (not tools) if v in ("none", None, False) else (v in tools)
            lines.append(_line("tool it ran", v, ", ".join(tools) or "none", ok))
        elif k == "asks_to_confirm":
            asked = act.get("asked_to_confirm") or []
            ok = (bool(asked) is v) if isinstance(v, bool) else (v in asked)
            lines.append(_line("asks you to confirm", v, ", ".join(asked) or "nothing", ok))
        elif k == "status":
            lines.append(_line("result", v, first.get("status") or "no task", first.get("status") == v))
        elif k == "reply_starts":
            lines.append(_line("reply starts", v, reply[:200], reply.startswith(str(v))))
        elif k in ("reply_has", "reply_not_has"):
            for x in (v if isinstance(v, list) else [v]):
                has = str(x).lower() in reply.lower()
                lines.append(_line("reply includes" if k == "reply_has" else "reply leaves out", x, reply[:200],
                                   has if k == "reply_has" else not has))
        elif k == "reply_matches":
            try:
                ok = re.search(str(v), reply) is not None
            except re.error:
                ok = False
            lines.append(_line("reply matches", v, reply[:200], ok))
        elif k == "result":
            for p, want in (v or {}).items():
                got = _path(first.get("result"), p)
                lines.append(_line(f"result {p}", want, got, got is not None and _same(got, want)))
        elif k == "result_has":
            for p, want in (v or {}).items():
                got = _path(first.get("result"), p)
                lines.append(_line(f"result {p} includes", want, got,
                                   got is not None and str(want).lower() in json.dumps(got, default=str).lower()))
        elif k == "confirmed":                     # the result itself says it happened (the same test as a plan step)
            from .admiral import unconfirmed
            why = unconfirmed(first.get("result")) if first.get("status") == "completed" else (first.get("status") or "no task")
            lines.append(_line("confirmed by what it reports", "confirmed" if v else "not confirmed",
                               "confirmed" if why is None else f"not confirmed: {why}", (why is None) is bool(v)))
        elif k == "error_has":
            err = str(first.get("error") or "") + " " + reply
            lines.append(_line("error includes", v, err.strip()[:200], str(v).lower() in err.lower()))
    return lines


async def _with(state: dict, coro):
    token = checkctx.CHECK.set(state)
    try:
        return await coro
    finally:
        checkctx.CHECK.reset(token)


async def _decline(app, out: dict) -> list[str]:
    """Every confirmation a check was asked for is declined (and never counted as his no: trust is untouched)."""
    from .assistant import PENDING_PREFIX
    asked = []
    for a in out.get("needs_confirmation") or []:
        asked.append(a.get("tool"))
        try:
            await app.state.redis.delete(PENDING_PREFIX + a["action_id"])
        except Exception:
            log.exception("couldn't drop a check's confirmation")
    return asked


async def _tasks_of(state: dict, ids: list[str], within: float) -> list[dict]:
    from .tasks import wait_for_result
    seen, out = set(), []
    for t in [*({"id": i, "tool": None} for i in ids), *state["tasks"]]:
        if t["id"] in seen:
            continue
        seen.add(t["id"])
        res = await wait_for_result(t["id"], within)
        tool = t["tool"] or await db.pool().fetchval("SELECT tool FROM task_runs WHERE id=$1", uuid.UUID(t["id"]))
        out.append({"tool": tool, "status": (res or {}).get("status") or "still running",
                    "error": (res or {}).get("error"), "result": (res or {}).get("result")})
    return out


async def _say(app, text: str, state: dict, within: float) -> dict:
    from .assistant import ask_for_check
    out = await _with(state, ask_for_check(app, text, uuid.uuid4().hex))
    asked = await _decline(app, out)
    tools = await _tasks_of(state, list(out.get("task_ids") or []), within)
    return {"route": out.get("route"), "reply": out.get("reply") or "", "tools": tools,
            "asked_to_confirm": sorted(set(asked) | set(state["holds"])), "would_act": state["blocked"],
            **({"model_called": False} if out.get("model_called") is False else {})}


async def _tool(app, tool: str, args: dict, state: dict, within: float) -> dict:
    from .tasks import StartTask, launch
    act: dict[str, Any] = {"tools": [], "asked_to_confirm": [], "would_act": []}
    try:
        tid = await _with(state, launch(app, StartTask(tool=tool, args=args, display="brief")))
    except HTTPException as exc:
        d = exc.detail if isinstance(exc.detail, dict) else {}
        if d.get("needs_confirmation"):
            act["asked_to_confirm"] = [tool]
        else:
            act["reply"] = str(d.get("reason") or exc.detail)
        act["would_act"] = state["blocked"]
        return act
    act["tools"] = await _tasks_of(state, [tid], within)
    return act


_FILLER = {"the", "my", "a", "an", "light", "lights", "lamp", "lamps", "door", "lock", "switch", "dimmer", "sensor", "tv"}


def _norm(name: Any) -> str:
    words = re.sub(r"[^\w ]+", " ", str(name or "").lower()).split()
    return " ".join(w for w in words if w not in _FILLER)


def _reported_names(actual: dict) -> list[str]:
    """The device names the check's own action reported (Orbit answers with the real one: "Kitchen")."""
    out: list[str] = []

    def walk(v, depth=0):
        if depth > 4:
            return
        if isinstance(v, dict):
            for k in ("name", "device"):
                if isinstance(v.get(k), str):
                    out.append(v[k])
            for x in v.values():
                walk(x, depth + 1)
        elif isinstance(v, list):
            for x in v[:20]:
                walk(x, depth + 1)
    for t in (actual or {}).get("tools") or []:
        walk(t.get("result"))
    return out


def _named(items: list, key: str, wanted: Any, reported: list[str]) -> tuple[dict | None, str]:
    """The item his words mean, matched the way Orbit matches names ("front door" is "Front Door Lock", "kitchen
    light" is "Kitchen"), or the name the check's action reported. Never a guess between two."""
    things = [it for it in items if isinstance(it, dict) and it.get(key) is not None]
    have = ", ".join(str(it[key]) for it in things[:12]) or "nothing"
    for r in reported:
        hit = [it for it in things if str(it[key]).lower() == r.lower()]
        if len(hit) == 1:
            return hit[0], ""
    w = _norm(wanted)
    if not w:
        return (things[0], "") if len(things) == 1 else (None, f"which {wanted}? (it has: {have})")
    for test in (lambda n: n == w, lambda n: set(w.split()) <= set(n.split()),
                 lambda n: w.replace(" ", "") in n.replace(" ", "")):
        hit = [it for it in things if test(_norm(it[key]))]
        if len(hit) == 1:
            return hit[0], ""
        if len(hit) > 1:
            return None, f"more than one called {wanted}: {', '.join(str(it[key]) for it in hit[:5])}"
    return None, f"nothing called {wanted} (it has: {have})"


async def _verify(app, v: Verify, names: dict, mode: str, since: datetime, found: dict, run_id: int,
                  reported: list[str] | None = None) -> list[dict]:
    """What the device (or his automation's run) reports afterwards, polled until it matches or time runs out.
    An automation counts only if it was made after the check started (the one he just made for it, never one of
    his own), and `found` remembers it so the clean-up deletes exactly that one."""
    deadline = time.monotonic() + _seconds(v.within)
    if v.automation:
        words = fill(app, v.automation, names)
        got = None
        while True:
            job = await db.pool().fetchrow(
                "SELECT id FROM jobs WHERE created_by='owner' AND NOT (rule ? 'once') AND title ILIKE '%' || $1 || '%' "
                "AND created_at >= $2 ORDER BY id DESC LIMIT 1", words, since)
            if job is not None:
                found["automation"] = job["id"]
                got = await db.pool().fetchrow(
                    "SELECT r.outcome, r.note, j.title FROM job_runs r JOIN jobs j ON j.id = r.job_id WHERE j.id=$1 "
                    "AND r.outcome <> 'skipped' ORDER BY r.id DESC LIMIT 1", job["id"])
            if got is not None or time.monotonic() > deadline:
                break
            await _touch(run_id)
            await asyncio.sleep(3)
        actual = f"{got['outcome']} ({got['title']}{': ' + got['note'] if got['note'] else ''})" if got else "it didn't run"
        return [_line(f"your automation \"{words}\" ran", v.outcome, actual,
                      got is not None and got["outcome"] == v.outcome)]
    tool, args, find = v.tool, fill(app, v.args, names), fill(app, v.find, names)
    want = fill(app, v.equals, names)
    seen: Any = "no answer"
    matched = None
    while True:
        res = await _tool(app, tool, args, checkctx.state(mode), 20)
        data = (res["tools"] or [{}])[0].get("result") if res["tools"] else None
        items = data.get(find.get("list")) if isinstance(data, dict) and find.get("list") else [data]
        crit = {k: x for k, x in find.items() if k != "list"}
        item, why = None, ""
        if isinstance(items, list):
            pool = [it for it in items if isinstance(it, dict)
                    and all(_same(it.get(k), x) for k, x in crit.items() if k != "name")]
            if "name" in crit:
                item, why = _named(pool, "name", crit["name"], reported or [])
            else:
                item = pool[0] if len(pool) == 1 else None
                why = "" if item else f"{len(pool)} match"
        if item is None and crit:
            seen = why if isinstance(items, list) else \
                (res.get("reply") or (res["tools"] or [{}])[0].get("error") or "no answer")
        elif item is not None:
            matched = item.get("name")
            seen = _path(item, v.field)
            if _same(seen, want):
                break
        if time.monotonic() > deadline:
            break
        await _touch(run_id)
        await asyncio.sleep(3)
    said = crit.get("name") if crit else tool
    shown = f"{said} ({matched})" if matched and str(matched).lower() != str(said).lower() else said   # "kitchen light (Kitchen)"
    label = f"{shown}: {v.field}" if v.field else str(tool)
    return [_line(f"{label} (as it reports)", want, seen, _same(seen, want))]


# ---- runs: a release's checks (or the ones he asked for) --------------------------------------------------

async def _row(rid: int) -> dict:
    return dict(await db.pool().fetchrow("SELECT * FROM check_results WHERE id=$1", rid))


async def _set(rid: int, **kw) -> None:
    jsonb = ("input", "expected", "actual", "lines")
    sets = ", ".join(f"{k}=${i}" + ("::jsonb" if k in jsonb else "") for i, k in enumerate(kw, 2))
    vals = [json.dumps(v, default=str) if k in jsonb else v for k, v in kw.items()]
    await db.pool().execute(f"UPDATE check_results SET {sets} WHERE id=$1", rid, *vals)


async def _touch(run_id: int, **kw) -> None:
    sets = ", ".join(f"{k}=${i}" for i, k in enumerate(kw, 2))
    await db.pool().execute(f"UPDATE check_runs SET updated_at=now(){', ' + sets if sets else ''} WHERE id=$1",
                            run_id, *kw.values())


async def create(app, title: str, paths: list[Path], by: str, release_id: int | None = None) -> int | None:
    """A run of these files' checks: each check a row (its status starts as what it needs). None if a run for
    this release already exists (two orchestrators, or the sweep and a retry)."""
    import asyncpg
    try:
        run_id = await db.pool().fetchval(
            "INSERT INTO check_runs (release_id, title, files, by_whom, status) VALUES ($1, $2, $3, $4, 'running') RETURNING id",
            release_id, title[:200], [p.name for p in paths], by)
    except asyncpg.UniqueViolationError:
        return None
    for p in paths:
        f, problem = load_file(p)
        if f is None:
            await db.pool().execute(
                "INSERT INTO check_results (run_id, file, check_id, name, needs, status, note) "
                "VALUES ($1, $2, '_file', $3, 'auto', 'error', $4)", run_id, p.name, f"{p.name} can't be used", problem)
            continue
        for i, c in enumerate(f.checks):
            await db.pool().execute(
                "INSERT INTO check_results (run_id, file, check_id, name, needs, status, ord, spec) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)", run_id, p.name, c.id, c.name[:200], needs(c),
                "pending" if needs(c) == "auto" else "needs_start", i, c.model_dump_json())
    return run_id


def _input_of(c: Check, filled: dict) -> dict:
    out = {}
    if c.needs_you:
        out["you"] = filled.get("needs_you")
    if c.say:
        out["say"] = filled.get("say")
    if c.tool:
        out["tool"] = c.tool
        out["args"] = filled.get("args")
    return out


def _expected_of(c: Check, filled: dict) -> dict:
    out = dict(filled.get("expect") or {})
    if c.verify:
        v = filled.get("verify") or {}
        out["verify"] = ({"automation": v.get("automation"), "outcome": v.get("outcome")} if v.get("automation") else
                         {"tool": v.get("tool"), "find": v.get("find"), "field": v.get("field"), "equals": v.get("equals")})
    if c.question:
        out["you answer"] = "yes"
    return out


async def _wait_answer(rid: int, asking: str, text: str, app, run_id: int) -> bool | None:
    """He answers on the panel (Yes/No, or Done/Couldn't). None: no answer in time."""
    await _set(rid, status="needs_you", asking=asking, question=text[:500], answer=None)
    await publish(app, run_id)
    deadline = time.monotonic() + ANSWER_WAIT
    while time.monotonic() < deadline:
        a = await db.pool().fetchval("SELECT answer FROM check_results WHERE id=$1", rid)
        if a is not None:
            await _set(rid, asking=None)
            return bool(a)
        await _touch(run_id)
        await asyncio.sleep(1)
    await _set(rid, asking=None)
    return None


async def run_one(app, run_id: int, r: dict, mode: str) -> None:
    """One check, start to finish: its row says what happened."""
    started, t0 = datetime.now(timezone.utc), time.monotonic()
    lines: list[dict] = []
    actual: dict[str, Any] = {}
    filled: dict[str, Any] = {}
    found: dict[str, Any] = {}
    try:
        c = Check.model_validate_json(r["spec"]) if isinstance(r["spec"], str) else Check.model_validate(r["spec"])
        names = home_vars(app)
        try:
            filled = {"say": fill(app, c.say, names) if c.say else None, "args": fill(app, c.args, names),
                      "expect": fill(app, c.expect, names),
                      "needs_you": fill(app, c.needs_you, names) if c.needs_you else None,
                      "question": fill(app, c.question, names) if c.question else None,
                      "verify": fill(app, c.verify.model_dump(), names) if c.verify else None,
                      "after": fill(app, c.after, names)}
        except Missing as exc:
            filled = {}
            await _set(r["id"], status="skipped", finished_at=datetime.now(timezone.utc),
                       note=f"Set \"{exc}\" in config/checks/{HOME_FILE} (your home's name for it).")
            return
        await _set(r["id"], status="running", started_at=started, input=_input_of(c, filled),
                   expected=_expected_of(c, filled))
        await publish(app, run_id)
        note, unanswered = None, False
        within = _seconds(c.within)
        if filled["needs_you"]:
            if c.verify:                            # what he does is checked by what it causes: shown while I watch
                await _set(r["id"], status="needs_you", asking="info", question=filled["needs_you"][:500])
                await publish(app, run_id)
            else:
                done = await _wait_answer(r["id"], "do", filled["needs_you"], app, run_id)
                if done is None:
                    await _set(r["id"], status="skipped", note="No answer in time.", finished_at=datetime.now(timezone.utc))
                    return
                lines.append(_line("you did it", "done", "done" if done else "couldn't", done))
        if c.say or c.tool:
            st = checkctx.state(mode, model=c.model)
            actual = await (_say(app, filled["say"], st, within) if c.say else _tool(app, c.tool, filled["args"], st, within))
            if actual.get("would_act") and mode == "auto":
                await _set(r["id"], status="needs_start", actual=actual, asking=None,
                           note=f"It would act ({', '.join(actual['would_act'])}), so it waits for your Start.")
                return
            lines += judge(filled["expect"], actual)
        if c.verify:
            if not filled["needs_you"]:
                await _set(r["id"], status="running", asking=None)
                await publish(app, run_id)
            lines += await _verify(app, c.verify, names, mode, started, found, run_id, _reported_names(actual))
            await _set(r["id"], status="running", asking=None)
        if filled["question"]:
            yes = await _wait_answer(r["id"], "ask", filled["question"], app, run_id)
            if yes is None:
                note, unanswered = "You didn't answer in time.", True
            lines.append(_line(filled["question"], "yes", {True: "yes", False: "no", None: "no answer"}[yes], yes is True))
        ok = bool(lines) and all(x["ok"] for x in lines)
        status = "passed" if ok else "skipped" if unanswered and all(x["ok"] for x in lines[:-1]) else "failed"
        await _set(r["id"], status=status, actual=_trim(actual, 4000), lines=lines, note=note, asking=None,
                   finished_at=datetime.now(timezone.utc), ms=int((time.monotonic() - t0) * 1000))
    except Exception as exc:
        log.exception("check %s/%s", r["file"], r["check_id"])
        await _set(r["id"], status="error", note=f"The check itself broke: {exc}"[:500], actual=_trim(actual),
                   lines=lines, asking=None, finished_at=datetime.now(timezone.utc), ms=int((time.monotonic() - t0) * 1000))
    finally:                                     # put things back, whatever happened (its result is already saved)
        if mode == "started" and (filled.get("after") or found):
            await _clean_up(app, filled.get("after") or [], r["id"], found)


async def _clean_up(app, steps: list[dict], rid: int, found: dict | None = None) -> None:
    """A check's "after": put things back (delete the test automation, turn the light back off). Never confirmed.
    {"delete": "automation"} deletes exactly the automation the check's verify found (the one made for the check)."""
    done = []
    for s in steps:
        if s.get("delete") == "automation":
            if (found or {}).get("automation"):
                from . import pulsar
                gone = await pulsar.delete(app, int(found["automation"]), "a field check's test automation")
                done.append("deleted the test automation" if gone else "the test automation couldn't be deleted")
            else:
                done.append("no test automation was made")
            continue
        try:
            st = checkctx.state("started")
            if s.get("say"):
                out = await _say(app, str(s["say"]), st, 20)
                done.append(f"said \"{s['say']}\": {out.get('reply') or 'ok'}"[:200])
            elif s.get("tool"):
                out = await _tool(app, str(s["tool"]), dict(s.get("args") or {}), st, 20)
                t = (out["tools"] or [{}])[0]
                done.append(f"{s['tool']}: {t.get('status') or out.get('reply') or 'asked to confirm'}")
        except Exception as exc:
            done.append(f"couldn't clean up: {exc}"[:200])
    cur = await db.pool().fetchval("SELECT note FROM check_results WHERE id=$1", rid)
    await _set(rid, note=((cur + " ") if cur else "") + "Cleaned up: " + "; ".join(done))


async def _heartbeat(run_id: int) -> None:
    while True:
        await asyncio.sleep(20)
        try:
            await _touch(run_id)
        except Exception:
            log.exception("check run %s heartbeat", run_id)


async def run_phase(app, run_id: int, mode: str) -> None:
    """Run the checks this phase can: on its own, the ones that only look; after his Start, the rest, in order.
    The run is kept fresh the whole time (a long wait is still a live run), and each check is claimed before it
    runs, so no check ever runs twice (two orchestrators, a Start pressed again)."""
    want = "pending" if mode == "auto" else "needs_start"
    beat = asyncio.create_task(_heartbeat(run_id))
    try:
        await publish(app, run_id, open_=True)
        for r in await db.pool().fetch("SELECT id FROM check_results WHERE run_id=$1 AND status=$2 ORDER BY file, ord",
                                       run_id, want):
            if mode == "started" and quiet(app):
                await _set(r["id"], note="Quiet hours started: this waits for your Start.")
                continue
            row = await db.pool().fetchrow("UPDATE check_results SET status='running' WHERE id=$1 AND status=$2 "
                                           "RETURNING *", r["id"], want)
            if row is None:
                continue                             # someone else has it
            await run_one(app, run_id, dict(row), mode)
            await _touch(run_id)
            await publish(app, run_id)
    except Exception:
        log.exception("check run %s", run_id)
    finally:
        beat.cancel()
    try:                                         # nothing left half-done: a check cut off is said to be
        await db.pool().execute(
            "UPDATE check_results SET status='error', asking=NULL, note=COALESCE(note, 'It stopped partway (see the log).') "
            "WHERE run_id=$1 AND status IN ('running','needs_you')", run_id)
        if mode == "auto":
            await db.pool().execute("UPDATE check_results SET status='needs_start', note='It didn't get to run on its own.' "
                                    "WHERE run_id=$1 AND status='pending'", run_id)
    except Exception:
        log.exception("check run %s cleanup", run_id)
    await settle(app, run_id)


async def settle(app, run_id: int) -> None:
    """The run's status from its checks, the report, and a notice if anything failed (once)."""
    rows = [dict(r) for r in await db.pool().fetch("SELECT status FROM check_results WHERE run_id=$1", run_id)]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    waiting = counts.get("needs_start", 0) + counts.get("needs_you", 0) + counts.get("pending", 0) + counts.get("running", 0)
    status = "waiting" if waiting else "done"
    await db.pool().execute("UPDATE check_runs SET status=$2, summary=$3::jsonb, updated_at=now(), "
                            "finished_at=CASE WHEN $2='done' THEN now() ELSE finished_at END WHERE id=$1",
                            run_id, status, json.dumps(counts))
    await publish(app, run_id, open_=True)
    bad = counts.get("failed", 0) + counts.get("error", 0)
    if bad:
        await _tell_failed(app, run_id, bad, len(rows))


async def _tell_failed(app, run_id: int, bad: int, total: int) -> None:
    from . import pulsar
    run = await db.pool().fetchrow("SELECT * FROM check_runs WHERE id=$1", run_id)
    names = [r["name"] for r in await db.pool().fetch(
        "SELECT name FROM check_results WHERE run_id=$1 AND status IN ('failed','error') ORDER BY file, ord LIMIT 4", run_id)]
    fix = " If it's broken, say \"roll back the last release\" (it asks you first)." if run["release_id"] else ""
    await pulsar.notice(app, "release_check_failed", f"checks:{run_id}:{bad}",
                        f"{bad} of {total} checks failed: {run['title']}"[:200],
                        ("Failed: " + "; ".join(names) + ". Say \"show the checks\" for what was expected and what "
                         "happened." + fix)[:1000])


def quiet(app) -> bool:
    from . import pulsar
    return pulsar.is_quiet(app)


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _live.add(t)
    t.add_done_callback(_live.discard)


async def start(app, run_id: int | None = None) -> dict:
    """His Start: the checks that act, now (never during quiet hours)."""
    if quiet(app):
        raise ValueError("It's quiet hours, so the checks that act wait until they end.")
    row = await db.pool().fetchrow(
        "UPDATE check_runs SET status='running', updated_at=now() WHERE id = COALESCE($1::bigint, (SELECT id FROM check_runs "
        "WHERE status='waiting' ORDER BY id DESC LIMIT 1)) AND status='waiting' AND EXISTS (SELECT 1 FROM check_results "
        "WHERE run_id=check_runs.id AND status='needs_start') RETURNING *", run_id)
    if row is None:
        raise ValueError("No checks are waiting for your Start.")
    n = await db.pool().fetchval("SELECT count(*) FROM check_results WHERE run_id=$1 AND status='needs_start'", row["id"])
    _spawn(run_phase(app, row["id"], "started"))
    return {"run": row["id"], "n": n, "title": row["title"]}


async def answer(rid: int, yes: bool, asking: str | None = None) -> str:
    """His tap: lands only on the prompt he saw (a late "Done" never answers the question that came after it)."""
    got = await db.pool().fetchval("UPDATE check_results SET answer=$2 WHERE id=$1 AND status='needs_you' "
                                   "AND asking IN ('ask','do') AND ($3::text IS NULL OR asking=$3) AND answer IS NULL "
                                   "RETURNING id", rid, yes, asking)
    if got is None:
        raise ValueError("That check isn't waiting for an answer.")
    return "Thanks."


# ---- after a release: its checks run (a sweep, so a missed report or a restart never loses them) --------

def _changed_checks(app, changed: list[str]) -> list[Path]:
    d = checks_dir(app)
    out = []
    for p in changed or []:
        m = re.fullmatch(r"config/checks/([^/]+\.ya?ml)", p)
        if m and m.group(1) != HOME_FILE and (d / m.group(1)).is_file():
            out.append(d / m.group(1))
    return sorted(set(out))


async def sweep(app, spec: dict) -> dict | None:
    """Each minute: a release that's been live a little while, with checks it added or changed, gets its run (once);
    a run cut off by a restart is closed, saying which checks didn't run."""
    for r in await db.pool().fetch(
            "SELECT id, message, changed_paths FROM releases r WHERE status='live' AND finished_at < now() - make_interval(secs => $1) "
            "AND finished_at > now() - interval '1 day' AND NOT EXISTS (SELECT 1 FROM check_runs c WHERE c.release_id = r.id) "
            "ORDER BY id", float(RELEASE_DELAY)):
        paths = _changed_checks(app, list(r["changed_paths"] or []))
        if not paths:
            continue
        title = f"Release #{r['id']}: " + ((r["message"] or "").splitlines() or [""])[0]
        run_id = await create(app, title, paths, "release", r["id"])
        if run_id is not None:
            _spawn(run_phase(app, run_id, "auto"))
    for run in await db.pool().fetch(            # claimed (fresh again), so only one orchestrator picks it up
            "UPDATE check_runs SET updated_at=now() WHERE status='running' AND updated_at < now() - make_interval(secs => $1) "
            "RETURNING id", float(STALE)):
        await db.pool().execute(
            "UPDATE check_results SET status='skipped', asking=NULL, "
            "note='Cut off by a restart: run the checks again to redo it.' WHERE run_id=$1 AND status IN ('running','needs_you')",
            run["id"])
        if await db.pool().fetchval("SELECT 1 FROM check_results WHERE run_id=$1 AND status='pending' LIMIT 1", run["id"]):
            _spawn(run_phase(app, run["id"], "auto"))       # its look-only checks still run on their own
        else:
            await settle(app, run["id"])
    return None


def _register_jobs() -> None:
    from . import engine
    engine.system("swallowtail.sweep", every=lambda app: 1)(sweep)


_register_jobs()


# ---- the report ----------------------------------------------------------------------------------------

ORDER = {"failed": 0, "error": 1, "needs_you": 2, "running": 3, "needs_start": 4, "pending": 5, "skipped": 6, "passed": 7}


def _j(v):
    return json.loads(v) if isinstance(v, str) else v


async def report(app, run_id: int) -> dict:
    run = await db.pool().fetchrow("SELECT * FROM check_runs WHERE id=$1", run_id)
    if run is None:
        raise ValueError("No such run of checks.")
    rows = [dict(r) for r in await db.pool().fetch("SELECT * FROM check_results WHERE run_id=$1 ORDER BY file, ord, id", run_id)]
    items = [{"id": r["id"], "file": r["file"], "check": r["check_id"], "name": r["name"], "status": r["status"],
              "input": _j(r["input"]) or {}, "expected": _j(r["expected"]) or {}, "actual": _j(r["actual"]) or {},
              "lines": _j(r["lines"]) or [], "note": r["note"], "ms": r["ms"],
              **({"asking": r["asking"], "question": r["question"]} if r["asking"] else {})} for r in rows]
    counts: dict[str, int] = {}
    for i in items:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    words = {"passed": "passed", "failed": "failed", "error": "broken", "needs_start": "wait for your Start",
             "needs_you": "need you", "skipped": "skipped", "running": "running", "pending": "to run"}
    parts = [f"{counts[k]} {words[k]}" for k in ("passed", "failed", "error", "needs_you", "needs_start", "running",
                                                  "pending", "skipped") if counts.get(k)]
    head = f"{run['title']}: " + (", ".join(parts) if parts else "no checks")
    return {"headline": head[:200], "checks": {
        "run": run["id"], "title": run["title"], "status": run["status"], "release": run["release_id"],
        "counts": counts, "items": sorted(items, key=lambda i: ORDER.get(i["status"], 9)),
        "can_start": bool(counts.get("needs_start")) and run["status"] == "waiting", "quiet": quiet(app)}}


async def publish(app, run_id: int, open_: bool = False) -> None:
    from .events import PanelEvent, publish as pub
    try:
        data = await report(app, run_id)
    except Exception:
        log.exception("check report %s", run_id)
        return
    tid = f"checks-{run_id}"
    if open_ and quiet(app):
        open_ = False                            # (overnight: the failure notice is held for him; the report waits)
    if open_:
        await pub(app.state.redis, PanelEvent(type="panel.open", task_id=tid, tool=REPORT_TOOL,
                                              data={"tier": "reference", "args": {}}))
    await pub(app.state.redis, PanelEvent(type="panel.result", task_id=tid, tool=REPORT_TOOL,
                                          data={"status": "completed", "partial": False, **data}))


async def summary_for_release(release_id: int) -> dict | None:
    """For her history of a release: how its field checks went."""
    run = await db.pool().fetchrow("SELECT * FROM check_runs WHERE release_id=$1 ORDER BY id DESC LIMIT 1", release_id)
    if run is None:
        return None
    failed = [r["name"] for r in await db.pool().fetch(
        "SELECT name FROM check_results WHERE run_id=$1 AND status IN ('failed','error') ORDER BY file, ord", run["id"])]
    return {"run": run["id"], "status": run["status"], "counts": _j(run["summary"]) or {}, "failed": failed[:10]}


# ---- his tools ----------------------------------------------------------------------------------------------

TOOLS = [
    registry.ToolSpec(
        name="checks", cancellable=False, display="panel", freshness_seconds=5,
        description="How a release's field checks went (the checks run in his home after a release goes live): each "
                    "check's input, what was expected, what actually happened, and its status. release: 'latest', its "
                    "number, or empty for the most recent run. Use for 'how did the checks go', 'did the last release "
                    "pass', 'what failed'.",
        input_schema={"type": "object", "properties": {"release": {"type": "string"}}}),
    registry.ToolSpec(
        name="run", cancellable=False,
        description="Run field checks now: which = 'latest' (the latest release's checks), a checks file's name "
                    "('pulsar'), or 'all'. The ones that only look run at once; the ones that act wait for his Start.",
        input_schema={"type": "object", "properties": {"which": {"type": "string"}}}),
    registry.ToolSpec(
        name="start", cancellable=False,
        description="Start the field checks that act (lights, computers, things he does), the ones waiting for his "
                    "Start. Never during quiet hours.",
        input_schema={"type": "object", "properties": {}}),
]


async def checks_tool(app, user: str, args: dict) -> dict:
    which = str(args.get("release") or "").strip().lower().lstrip("#")
    if which in ("", "latest", "last", "newest"):
        rid = await db.pool().fetchval("SELECT id FROM check_runs ORDER BY id DESC LIMIT 1") if not which else \
            await db.pool().fetchval("SELECT c.id FROM check_runs c JOIN releases r ON r.id = c.release_id "
                                     "ORDER BY r.id DESC, c.id DESC LIMIT 1")
    elif which.isdigit():
        rid = await db.pool().fetchval("SELECT id FROM check_runs WHERE release_id=$1 ORDER BY id DESC LIMIT 1", int(which))
    else:
        rid = await db.pool().fetchval("SELECT id FROM check_runs WHERE $1 = ANY(files) OR lower(title) LIKE '%' || $2 || '%' "
                                       "ORDER BY id DESC LIMIT 1", f"{which}.yaml", which)
    if rid is None:
        raise ValueError("No field checks have run for that yet. Say \"run the checks\" to run them.")
    return await report(app, rid)


async def run_tool(app, user: str, args: dict) -> dict:
    which = re.sub(r"^(?:the|my)\s+|\s+(?:checks?|tests?)$", "", str(args.get("which") or "latest").strip().lower()).strip()
    every = files(app)
    if not every:
        raise ValueError("There are no checks files yet (config/checks/).")
    release_id = None
    if which in ("", "latest", "last", "release", "the release", "last release", "latest release"):
        row = await db.pool().fetchrow("SELECT id, message, changed_paths FROM releases WHERE status='live' ORDER BY id DESC LIMIT 1")
        paths = _changed_checks(app, list(row["changed_paths"] or [])) if row else []
        if not paths:
            raise ValueError("The latest release didn't add or change any checks. Say \"run all the checks\", or name "
                             "one: " + ", ".join(p.stem for p in every) + ".")
        title = f"Release #{row['id']} (again): " + ((row["message"] or "").splitlines() or [""])[0]
    elif which in ("all", "every", "everything"):
        paths, title = every, "All the checks"
    else:
        key = re.sub(r"[^a-z0-9]+", "", which)
        paths = [p for p in every if key and key in re.sub(r"[^a-z0-9]+", "", p.stem.lower())]
        if not paths:
            for p in every:                          # its title ("Pulsar (Phase 10)")
                f, _ = load_file(p)
                if f and key and key in re.sub(r"[^a-z0-9]+", "", f.release.lower()):
                    paths.append(p)
        if not paths:
            raise ValueError(f"I don't have checks called \"{which}\". Mine: " + ", ".join(p.stem for p in every) + ".")
        title = "Checks: " + ", ".join(p.stem for p in paths)
    run_id = await create(app, title, paths, "you", release_id)
    _spawn(run_phase(app, run_id, "auto"))
    n = await db.pool().fetchval("SELECT count(*) FROM check_results WHERE run_id=$1", run_id)
    return {"headline": f"Running {n} check{'s' if n != 1 else ''}",
            "did": {"node": "Swallowtail", "what": f"Running {n} check{'s' if n != 1 else ''} ({title}). The ones that "
                                                     "only look run now; the report shows which wait for your Start."}}


async def start_tool(app, user: str, args: dict) -> dict:
    out = await start(app)
    return {"headline": f"Starting {out['n']} checks", "did": {"node": "Swallowtail", "what":
            f"Starting the {out['n']} check{'s' if out['n'] != 1 else ''} that act ({out['title']}). Their report shows each one."}}


HANDLERS = {"checks": checks_tool, "run": run_tool, "start": start_tool}


# ---- the panel's buttons (a TV's screen can't press these) ----------------------------------------------------

async def _from_display(request: Request) -> None:
    dev = await auth.asker(request.headers)
    if dev is not None and dev["kind"] == "screen":
        raise HTTPException(403, "A TV's screen can't do that.")


class Answer(BaseModel):
    yes: bool
    asking: str | None = None          # which prompt he answered ("ask" or "do")


@router.post("/runs/{run_id}/start")
async def start_http(run_id: int, request: Request):
    await _from_display(request)
    try:
        out = await start(request.app, run_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True, "done": f"Starting {out['n']} check{'s' if out['n'] != 1 else ''}."}


@router.post("/results/{rid}/answer")
async def answer_http(rid: int, body: Answer, request: Request):
    await _from_display(request)
    try:
        return {"ok": True, "done": await answer(rid, body.yes, body.asking)}
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.get("/runs/{run_id}/panel")
async def run_panel(run_id: int, request: Request):
    """What a notification about a run opens (the report, as it is now)."""
    try:
        data = await report(request.app, run_id)
    except ValueError:
        raise HTTPException(404, "No such run of checks.")
    return {"task_id": f"checks-{run_id}", "tool": REPORT_TOOL, "data": {"status": "completed", "partial": False, **data}}


# ---- registration and the function contract (like Pulsar's) -------------------------------------------------

async def register_loop(app) -> None:
    from .settings import settings
    while True:
        ttl = app.state.config.current.registry.function_ttl_seconds
        try:
            await registry.register(app.state.redis, registry.FunctionRegistration(
                name=NAME, description="Field checks of each release, run in his home after it goes live.",
                url=f"{settings.self_url.rstrip('/')}/{NAME}", tools=TOOLS), ttl)
        except Exception:
            log.exception("couldn't register Swallowtail; will retry")
        await asyncio.sleep(max(3, ttl // 3))


class Invoke(BaseModel):
    task_id: str
    args: dict = {}


@router.post("/tools/{tool}/invoke", status_code=202)
async def invoke(tool: str, body: Invoke, request: Request):
    if tool not in HANDLERS:
        raise HTTPException(404, f"{NAME} has no tool '{tool}'")
    _spawn(_run(request.app, tool, body.task_id, body.args))
    return {}


@router.post("/cancel/{task_id}", status_code=202)
async def cancel(task_id: str):
    return {}


@router.get("/health")
async def health():
    return {"status": "ok"}


async def _run(app, tool: str, task_id: str, args: dict) -> None:
    from . import tasks
    try:
        row = await db.pool().fetchrow("SELECT user_id FROM task_runs WHERE id=$1", uuid.UUID(task_id))
        data = await HANDLERS[tool](app, row["user_id"] if row else "owner", {**args, "_self": task_id})
        result = tasks.Result(status="completed", data=data)
    except ValueError as exc:
        result = tasks.Result(status="failed", error=str(exc))
    except Exception as exc:
        log.exception("swallowtail.%s failed", tool)
        result = tasks.Result(status="failed", error=f"Something went wrong: {exc}")
    await tasks._finish(tasks._AppContext(app), task_id, f"{NAME}.{tool}", result)
