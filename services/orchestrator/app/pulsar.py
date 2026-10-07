"""Pulsar (Phase 10): his automations, routines, and knowing when something is worth interrupting him.

  - Automations: something that repeats, set up in his words ("every weekday at 7 am, lock the front door",
    "turn off the lights every night at 11", "remind me every Sunday at 6 pm to take the trash out", "when the
    TV PC comes online, open YouTube on it"). Code reads the time and settles the command (the same fast path
    as saying it now); I read it back and it's saved only when he confirms. Each run is one of Admiral's
    engine jobs: governed when it runs (a change-tier action asks him each time, unless he's let me earn trust
    for exactly that), a heads-up with Cancel before physical actions, checked by its own result, never run
    twice. A run that worked goes in the digest, not on his phone; a failure tells him; three in a row and
    it's paused until he resumes it. Pause, resume, skip the next one, delete: all in words or on its panel.
  - Routines: several commands under one name ("make a routine called goodnight: lock the front door, turn off
    the living room lights and turn off the TV"). Each command is settled by its own fast path, he confirms
    the list, and it's kept as a skill (Morpho's) that needs no model: saying its name runs it, and it can be
    an automation's command ("run goodnight every night at 11").
  - Events: an automation can wait for something I already see happen: a computer coming online or going
    offline, a display opening. Doors, lights and arrivals come with the Home Assistant bridge (Lagrange).
  - Initiative: I watch for what he'd want to know (a door unlocked during quiet hours, a water leak, the
    server running out of disk, a server-role computer down, a failed update, an automation paused, the model
    budget running ahead, mailbox items waiting). Each kind's severity is his, in governance (pulsar.yaml):
      critical  his phone and every open display, even in quiet hours
      high      his phone now; during quiet hours, held until they end
      normal    the mailbox and his displays, no phone; in the digest
      low       the digest only
    He hears about a thing once; again only after it clears and comes back, or after the repeat interval he
    set, and never again once he's said "got it" (until it clears). Everything goes in the morning digest.
  - Suggestions: Parallax's habits ("you lock the door around 11 most nights") are mailbox cards; approving
    one makes the automation. I never create, approve or schedule an automation on my own.
"""
import asyncio
import json
import logging
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import auth, db, registry, when

log = logging.getLogger("aurelia.pulsar")
router = APIRouter(prefix="/pulsar", tags=["pulsar"])
NAME = "pulsar"
DRAFT = "aurelia:pulsar:draft:{}"
DRAFT_TTL = 3600
SNAP = "aurelia:pulsar:seen"                 # last look: which computers were online, which displays open
OFFLINE_SINCE = "aurelia:pulsar:offline_since"
CHECKS = "aurelia:pulsar:checks"             # each watcher's last result, for the notices panel
THROTTLE = "aurelia:pulsar:throttle:{}"
LEVELS = ("critical", "high", "normal", "low")
SYSTEM_USER = "pulsar"                        # the home checks run as this (charter.yaml): it only looks

RULES: dict[str, Any] = {
    "min_every_minutes": 15,
    "pause_after_failures": 3,
    "event_cooldown_minutes": 10,
    "quiet_hours": None,                      # None: the ambient screen's quiet hours (config/aurelia.yaml)
    "severity": {
        "door_unlocked_at_night": "critical", "water_leak": "critical", "server_disk_low": "critical",
        "computer_down": "high", "update_failed": "high", "automation_paused": "high", "release_check_failed": "high",
        "budget_ahead": "normal", "mailbox_waiting": "normal", "automations_ready": "normal",
        "battery_low": "low",
    },
    "repeat_hours": {"critical": 1, "high": 24, "normal": 0, "low": 0},    # 0: once, until it clears
    "ntfy_for": ["critical"],                 # also sent to your ntfy (Beacon), a second path to your phone
    "watch": {"server_disk_free_percent": 10, "computer_down_minutes": 10, "always_on": [],
              "mailbox_waiting_hours": 24, "home_check_minutes": 10, "battery_low_percent": 15},
}
WATCHING = {                                  # what each kind is, for the notices panel
    "door_unlocked_at_night": "A door left unlocked during quiet hours",
    "water_leak": "A leak sensor that's wet",
    "server_disk_low": "The server running out of disk",
    "computer_down": "A server (or a computer you listed as always on) down",
    "update_failed": "An update of mine that failed or was rolled back",
    "automation_paused": "An automation paused after failing",
    "release_check_failed": "A release's field checks that failed (Swallowtail)",
    "budget_ahead": "The model budget running out before the month ends",
    "mailbox_waiting": "Mailbox items waiting more than a day",
    "battery_low": "A lock or sensor with a low battery",
}
ONE_SHOT = ("update_failed", "automation_paused", "automations_ready", "release_check_failed")   # events, not states: cleared after a day
NOT_AUTOMATED = ("admiral.", "pulsar.", "morpho.")
_STOP = {"the", "my", "a", "an", "one", "that", "this", "automation", "automations", "routine", "routines", "next",
         "to", "for", "at", "of", "on", "run", "runs", "again", "every", "each", "tomorrow's", "today's", "tonight's",
         "tomorrows", "todays", "tonights", "tomorrow", "today", "tonight", "once", "time", "please"}


def rules(app) -> dict:
    pol = app.state.governance.functions.get(NAME)
    got = (pol.rules if pol else None) or {}
    out = {**RULES, **{k: v for k, v in got.items() if k not in ("severity", "repeat_hours", "watch")}}
    for k in ("severity", "repeat_hours", "watch"):
        out[k] = {**RULES[k], **(got.get(k) or {})}
    return out


def level(app, kind: str) -> str:
    lv = str(rules(app)["severity"].get(kind) or "normal").lower()
    return lv if lv in LEVELS else "normal"


def quiet_spec(app) -> str:
    spec = rules(app).get("quiet_hours")
    if spec is None:
        try:
            spec = app.state.config.current.ambient.quiet_hours
        except Exception:
            spec = ""
    return str(spec or "")


def is_quiet(app, at: datetime | None = None) -> bool:
    from .ambient import in_quiet
    from . import engine
    local = (at or datetime.now(timezone.utc)).astimezone(ZoneInfo(engine.tz_name(app)))
    return in_quiet(quiet_spec(app), local.time())


def _sentence(s: str) -> str:
    s = " ".join(str(s or "").split()).strip(" .,")
    return s[:1].upper() + s[1:]


def _clean(text: str) -> str:
    from . import fastpath
    t = " ".join(str(text or "").strip().rstrip(".!?").split())
    return fastpath._POLITE.match(t).group("rest") or t


# ---- tools ----------------------------------------------------------------------------------------------

TOOLS = [
    registry.ToolSpec(
        name="automate", cancellable=False, display="panel",
        description="Set up an automation: one of your commands, a routine or a reminder that repeats, by the clock "
                    "('every weekday at 7 am, lock the front door', 'turn off the lights every night at 11', 'remind me "
                    "every Sunday at 6 pm to take the trash out', 'every 2 hours…', 'on the 1st of every month at 9…') "
                    "or when something happens ('when the TV PC comes online, open YouTube on it'). say: his words in "
                    "full; the time and the command are read by code. It's only a draft until he confirms the read-back "
                    "on the display. Only when he asks; never on your own. For something once, use admiral.schedule.",
        input_schema={"type": "object", "properties": {"say": {"type": "string"}}, "required": ["say"]}),
    registry.ToolSpec(
        name="save", cancellable=False,
        description="Saves a confirmed automation or routine draft. Only his confirmation of a read-back runs it.",
        input_schema={"type": "object", "properties": {"draft": {"type": "string"}}, "required": ["draft"]}),
    registry.ToolSpec(
        name="automations", cancellable=False, display="panel", freshness_seconds=10,
        description="His automations (each with how it repeats, its next run and how its last runs went, with Pause, "
                    "Skip next and Delete) and his routines (each with its steps and the words that run it).",
        input_schema={"type": "object", "properties": {}}),
    registry.ToolSpec(name="pause", cancellable=False,
                      description="Pause one of his automations until he resumes it. which: his words for it.",
                      input_schema={"type": "object", "properties": {"which": {"type": "string"}}}),
    registry.ToolSpec(name="resume", cancellable=False,
                      description="Resume a paused automation (from its next time; what it missed isn't run).",
                      input_schema={"type": "object", "properties": {"which": {"type": "string"}}}),
    registry.ToolSpec(name="skip", cancellable=False,
                      description="Skip an automation's next run only ('skip tomorrow's lock automation').",
                      input_schema={"type": "object", "properties": {"which": {"type": "string"}}}),
    registry.ToolSpec(name="delete", cancellable=False,
                      description="Delete one of his automations for good. which: his words for it.",
                      input_schema={"type": "object", "properties": {"which": {"type": "string"}}}),
    registry.ToolSpec(
        name="routine", cancellable=False, display="panel",
        description="Make a routine: several of your commands under one name, run by saying it ('make a routine called "
                    "goodnight: lock the front door, turn off the living room lights and turn off the TV'). say: his "
                    "words in full. Each command is settled by code; it's a draft until he confirms the read-back.",
        input_schema={"type": "object", "properties": {"say": {"type": "string"}}, "required": ["say"]}),
    registry.ToolSpec(name="forget_routine", cancellable=False,
                      description="Delete one of his routines (and any automation that runs it). which: its name.",
                      input_schema={"type": "object", "properties": {"which": {"type": "string"}}, "required": ["which"]}),
    registry.ToolSpec(
        name="notices", cancellable=False, display="panel", freshness_seconds=10,
        description="What you're watching for him and how each is told (its severity, his to set), what's open now "
                    "(a door unlocked, a server down…) and what cleared lately.",
        input_schema={"type": "object", "properties": {}}),
]
INTERNAL = ("pulsar.save",)                   # never offered to the model: only his confirmation of a read-back


# ---- reading an automation from his words -----------------------------------------------------------------

_REMIND = re.compile(r"^(?:please\s+)?remind\s+me\s+(?P<rest>.+)$", re.I)
_LEAD = re.compile(r"^(?:to|that|about|of)\s+", re.I)
_EV = (r"(?P<ev>comes?\s+(?:back\s+)?(?:online|on)|is\s+(?:back\s+)?(?:online|on)|turns?\s+on|wakes?(?:\s+up)?|"
       r"boots?(?:\s+up)?|starts?(?:\s+up)?|goes\s+(?:offline|off|to\s+sleep)|turns?\s+off|shuts?\s+down|"
       r"is\s+(?:offline|off)|opens|is\s+opened|gets\s+opened)")
_WHEN = r"(?:when(?:ever)?|each\s+time|every\s+time|any\s+time)"
from .happenings import _COND as _EV_COND           # "between 5pm and 6pm", "on weekdays", "during quiet hours"
EVENT_FIRST = re.compile(rf"^{_WHEN}\s+(?:i\s+open\s+(?:the\s+|my\s+)?(?P<dev2>[\w' -]+?)|(?:the\s+|my\s+)?(?P<dev>[\w' -]+?)\s+{_EV})"
                         rf"{_EV_COND}\s*,?\s+(?P<what>.+)$", re.I)
EVENT_LAST = re.compile(rf"^(?P<what>.+?)\s+{_WHEN}\s+(?:i\s+open\s+(?:the\s+|my\s+)?(?P<dev2>[\w' -]+?)|"
                        rf"(?:the\s+|my\s+)?(?P<dev>[\w' -]+?)\s+{_EV}){_EV_COND}$", re.I)


def _event_kind(ev: str | None) -> str:
    if ev is None:
        return "open"
    e = ev.lower()
    if "open" in e:
        return "open"
    if re.search(r"off|sleep|down", e):
        return "offline"
    return "online"


async def _event_device(app, words: str, kind: str) -> tuple[int, str, str]:
    """The computer (online/offline) or display (open) his words name. Never a guess between two."""
    from .constellation import _match, displays
    if kind == "open":
        every = await displays(app)
        hits = [d for d in every if _match(words, d["name"], d.get("hostname"))]
        if not hits and set(re.findall(r"[a-z]+", words.lower())) <= {"my", "the", "phone", "iphone", "android", "cell", "mobile"}:
            hits = [d for d in every if d["kind"] == "browser" and (d["os"] in ("iOS", "Android") or d["role"] == "companion")]
        if len(hits) != 1:
            names = ", ".join(sorted({d["name"] for d in every})) or "none yet"
            raise ValueError(f"Which display? Yours: {names}." if hits else f"I don't know a display called \"{words}\". "
                             f"Yours: {names}.")
        return hits[0]["id"], hits[0]["name"], "display"
    rows = await db.pool().fetch("SELECT id, name, info FROM devices WHERE kind='node' AND status='approved'")
    ns = [(r["id"], r["name"], (json.loads(r["info"]) if isinstance(r["info"], str) else (r["info"] or {})).get("hostname"))
          for r in rows]
    hits = [n for n in ns if _match(words, n[1], n[2])]
    if len(hits) != 1:
        names = ", ".join(n[1] for n in ns) or "none yet"
        raise ValueError(f"Which computer: {', '.join(h[1] for h in hits)}?" if hits else
                         f"I don't know a computer called \"{words}\". Yours: {names}.")
    return hits[0][0], hits[0][1], "computer"


def _patterns(app, routine: bool = False) -> list:
    pats = app.state.config.current.fastpath.patterns
    return [p for p in pats if not p.tool.startswith(NOT_AUTOMATED) and not p.name.startswith(("schedule-", "automate-"))]


async def _command(app, what: str, verbs: bool = False) -> tuple[str, dict, str]:
    """His words for one command -> exactly what will run (its own fast path), or ValueError."""
    from . import admiral, fastpath
    t = _clean(what)
    cands = [t]
    if verbs:                                    # "that locks the door and turns off the lights"
        first, _, rest = t.partition(" ")
        for base in (first[:-2] if first.endswith(("ches", "shes", "sses", "xes")) else None,
                     first[:-1] if first.endswith("s") and not first.endswith("ss") else None):
            if base:
                cands.append(f"{base} {rest}".strip())
    for c in cands:
        m = fastpath.match(c, _patterns(app))
        if m is None:
            continue
        tool = m.pattern.tool
        resolved = await registry.resolve(app.state.redis, tool)
        if resolved is None:
            raise ValueError(f"{tool} isn't available right now, so I can't set that up.")
        tier = admiral.tier_of(app, tool)
        if tier == "never":
            raise ValueError(f"\"{t}\" is never allowed, so I won't automate it.")
        return tool, fastpath.coerce(m.args, resolved.tool.input_schema or {}), _sentence(t)
    from .assistant import _lookup_elsewhere          # "send my youtube to my phone": a look-up on another display
    away = None
    for c in cands:                                    # ("that sends my youtube …" too)
        away = await _lookup_elsewhere(app, c)
        if away is not None:
            break
    if away is not None:
        look, largs, to, what = away
        if await registry.resolve(app.state.redis, "constellation.show") is None:
            raise ValueError("Showing things on another display isn't available right now, so I can't set that up.")
        return "constellation.show", {"tool": look, "args": largs, "to": to, "what": what}, _sentence(t)
    raise ValueError(f"I can't tell exactly which command \"{t}\" is. Say it the way you'd ask me right now "
                     "(like \"lock the front door\").")


async def _routine_named(app, words: str) -> dict | None:
    """A routine (or any skill that needs no model) he named: "goodnight", "run goodnight", "the goodnight routine"."""
    from . import morpho
    t = re.sub(r"^(?:run|start|do|use)\s+", "", _clean(words), flags=re.I)
    t = re.sub(r"^(?:the|my)\s+", "", t, flags=re.I)
    t = re.sub(r"\s+(?:routine|skill)$", "", t, flags=re.I).strip()
    for sk in await morpho.active_skills():
        if sk["name"].lower() == t.lower() and morpho.model_free(sk) and not sk["slots"]:
            return sk
    hit = await morpho.match_phrase(app, words)
    if hit and morpho.model_free(hit[0]) and not hit[0]["slots"]:
        return hit[0]
    return None


async def _what(app, what: str) -> tuple[str, str, dict, list[str]]:
    """(kind, title, spec, notes) for what an automation runs: a reminder, a routine, or one command."""
    from . import admiral, engine
    w = what.strip(" ,.")
    if not w:
        raise ValueError("What should I do then?")
    m = _REMIND.match(w)
    if m:
        text = _sentence(_LEAD.sub("", m.group("rest")))
        if not text:
            raise ValueError("Remind you of what?")
        return "reminder", text, {"text": text}, []
    sk = await _routine_named(app, w)
    if sk is not None:
        tools = [s["tool"] for s in sk["steps"]]
        notes = ["Steps that need your OK ask each time, on its panel."] if any(
            admiral.tier_of(app, t) == "change" for t in tools) else []
        return "routine", f"Run {sk['name']}", {"skill_id": sk["id"], "name": sk["name"], "tools": tools}, notes
    tool, args, title = await _command(app, w)
    notes = []
    if admiral.tier_of(app, tool) == "change":
        notes.append("It asks for your OK each time it's due (in your mailbox and on your phone).")
    return "tool", title, {"tool": tool, "args": args}, notes


async def parse(app, say: str, user: str = "owner") -> dict:
    """His words -> a draft automation (nothing is saved). ValueError with a plain question when it isn't clear."""
    from . import engine
    t = _clean(say)
    if not t:
        raise ValueError("What should I automate?")
    from . import happenings
    m = EVENT_FIRST.match(t) or EVENT_LAST.match(t)
    home = None
    if m:                                       # a computer or display first; then the home's devices and hooks
        try:
            kind_ev = _event_kind(m.group("ev"))
            dev_words = (m.group("dev2") or m.group("dev") or "").strip()
            dev_id, dev_name, dev_kind = await _event_device(app, dev_words, kind_ev)
        except ValueError as exc:
            if not str(exc).startswith("I don't know"):     # "which computer?": his answer, never a guess elsewhere
                raise
            home = await happenings.parse(app, t)
            if home is None:
                raise
    else:
        home = await happenings.parse(app, t)
    try:
        if home:
            rule, what = home
        elif m:
            rule = {"on": kind_ev, "device": dev_id, "device_name": dev_name, **happenings.conditions(m.group("cond"))}
            what = m.group("what")
        else:
            rm = _REMIND.match(t)
            if rm:
                rule, rest, _ = when.split_recurring(rm.group("rest"))
                what = "remind me " + rest
            else:
                rule, what, _ = when.split_recurring(t)
    except when.Unclear as exc:
        raise ValueError(str(exc))
    if rule.get("on"):                          # an event automation: when it may run is said right after the event
        stray = happenings.condition_in(what)
        if stray:
            raise ValueError(f"Say when it may run right after what happens, not in what to do: for example "
                             f"“when the front door unlocks {stray}, …”.")
    r = rules(app)
    if rule.get("every_minutes") and float(rule["every_minutes"]) < float(r["min_every_minutes"]):
        raise ValueError(f"That's too often: at most every {r['min_every_minutes']} minutes.")
    kind, title, spec, notes = await _what(app, what)
    job = {"kind": kind, "spec": spec, "rule": rule, "tz": engine.tz_name(app), "heads_up": engine.heads_for(app, kind, spec),
           "created_by": "owner"}
    rep = engine.repeats(job) or ""
    if job["heads_up"]:
        hs = int(engine.rules(app)["heads_up_seconds"])
        notes.insert(0, f"You'll get a heads-up {hs // 60 or hs} {'minute' if hs >= 60 else 'seconds'} before each run, "
                        "with a Cancel.")
    first = None
    if not rule.get("on"):
        _, planned, _, _ = engine.slot(app, job, engine.now())
        first = when.say(planned.astimezone(ZoneInfo(job["tz"])), engine.now())
    readback = f"{rep[:1].upper() + rep[1:]}: {title}."
    return {"type": "automation", "user": user, "kind": kind, "title": title, "spec": spec, "rule": rule,
            "repeats": rep, "first": first, "notes": notes, "heads_up": job["heads_up"], "readback": readback}


async def fast_ok(app, say: str) -> bool:
    """Whether an automation fast path really is one (a time or event read by code, and a command I know).
    Otherwise the model hears it: never another fast path, which could run it now."""
    try:
        await parse(app, say)
        return True
    except Exception:
        return False


# ---- routines ---------------------------------------------------------------------------------------------

ROUTINE = [
    re.compile(r"^(?:make|create|add|set\s+up|save|build)\s+(?:me\s+)?(?:a\s+|an\s+|the\s+)?(?:new\s+)?routine\s+"
               r"(?:called|named)\s+[\"“']?(?P<name>[^:,\"”']+?)[\"”']?\s*(?::|,|-|—|\s+that\s+|\s+which\s+|\s+to\s+)\s*"
               r"(?P<steps>.+)$", re.I),
    re.compile(r"^(?:make|create|add|set\s+up|save|build)\s+(?:me\s+)?(?:a\s+|an\s+|my\s+)?[\"“']?(?P<name>[\w' -]+?)[\"”']?"
               r"\s+routine\s*(?::|,|-|—|\s+that\s+|\s+which\s+|\s+to\s+)\s*(?P<steps>.+)$", re.I),
]
_SEP = re.compile(r"(\s*;\s*|\s*,\s*(?:and\s+)?(?:then\s+)?|\s+(?:and\s+)?then\s+)", re.I)


async def parse_routine(app, say: str, user: str = "owner") -> dict:
    from . import admiral, fastpath, morpho
    t = _clean(say)
    m = next((rx.match(t) for rx in ROUTINE if rx.match(t)), None)
    if m is None:
        raise ValueError("Say it like \"make a routine called goodnight: lock the front door, turn off the living room "
                         "lights and turn off the TV\".")
    name = " ".join(m.group("name").split()).strip(" .")[:40]
    if not name or not re.search(r"[a-z0-9]", name, re.I):
        raise ValueError("What should the routine be called?")
    third = bool(re.search(r"\b(?:that|which)\s*$", t[:m.start("steps")], re.I))
    parts = _SEP.split(m.group("steps").strip())
    chunks: list[tuple[str, bool]] = []          # (words, runs after the one before it)
    then = False
    for i, p in enumerate(parts):
        if i % 2:
            then = "then" in p.lower()
            continue
        if p.strip():
            chunks.append((p.strip(" ."), then))
            then = False
    steps = []
    for words, after in chunks:
        # "peek the shed and flip the porch": two commands when each part is one on its own (a broad command
        # could swallow the rest); otherwise the words are one command ("play Tom and Jerry on the TV").
        bits = [b for b in re.split(r"\s+and\s+", words, flags=re.I) if b.strip()]
        split = []
        if len(bits) > 1:
            for b in bits:
                try:
                    split.append(await _command(app, b, verbs=third))
                except ValueError:
                    split = []
                    break
        if split:
            steps += [(c, after if j == 0 else False) for j, c in enumerate(split)]
            continue
        try:
            steps.append((await _command(app, words, verbs=third), after))
        except ValueError:
            raise ValueError(await _why_not(app, words, third))
    if not steps:
        raise ValueError("What should the routine do?")
    if len(steps) > admiral.MAX_STEPS:
        raise ValueError(f"A routine can have at most {admiral.MAX_STEPS} steps.")
    if any(s["name"].lower() == name.lower() for s in await morpho.active_skills()):
        raise ValueError(f"You already have a routine or skill called \"{name}\". Pick another name.")
    out_steps, notes = [], []
    for i, ((tool, args, title), after) in enumerate(steps, 1):
        out_steps.append({"n": i, "title": title, "tool": tool, "args": args, "depends": [i - 1] if after and i > 1 else [],
                          "fill": False})
        if admiral.tier_of(app, tool) == "change":
            notes.append(f"Step {i} ({title}) asks for your OK each time.")
    phrases = []
    for ph in (name.lower(), f"run {name.lower()}", f"start {name.lower()}", f"run the {name.lower()} routine",
               f"run my {name.lower()} routine"):
        m = fastpath.match(ph, app.state.config.current.fastpath.patterns)
        if (m is not None and m.pattern.name != "routine-run") or await morpho.match_phrase(app, ph):
            continue                             # already one of my commands, or another skill's words
        phrases.append(ph)
    if not phrases:
        raise ValueError(f"\"{name}\" already means something else to me. Pick another name.")
    if phrases[0] != name.lower():
        notes.append(f"\"{name}\" alone already means something else to me, so say \"{phrases[0]}\".")
    listing = "; ".join(f"{s['n']}) {s['title']}" + (" (after the one before)" if s["depends"] else "") for s in out_steps)
    return {"type": "routine", "user": user, "name": name, "steps": out_steps, "phrases": phrases, "notes": notes,
            "readback": f"Routine \"{name}\": {listing}. Say \"{phrases[0]}\" to run it."}


async def _why_not(app, words: str, third: bool) -> str:
    for b in [words, *re.split(r"\s+and\s+", words, flags=re.I)]:
        try:
            await _command(app, b, verbs=third)
        except ValueError as exc:
            return str(exc)
    return f"I can't tell exactly which command \"{words}\" is."


async def routine_fast_ok(app, say: str) -> bool:
    try:
        await parse_routine(app, say)
        return True
    except Exception:
        return False


# ---- drafts and saving --------------------------------------------------------------------------------------

async def _draft(app, d: dict) -> str:
    did = uuid.uuid4().hex
    await app.state.redis.set(DRAFT.format(did), json.dumps(d), ex=DRAFT_TTL)
    return did


READBACK_MAX = 900                            # what the confirmation card shows, whole (assistant._follow_up)


def _readback(d: dict) -> str:
    notes = " ".join(d.get("notes") or [])
    first = f" First run: {d['first']}." if d.get("first") else ""
    text = f"{d['readback']}{first}{' ' + notes if notes else ''} Save it?"
    if len(text) > READBACK_MAX:                 # he confirms only what he can read in full
        raise ValueError("That's too long for me to read back in full. Split it into two (two routines, or shorter "
                         "steps).")
    return text


def _confirm(did: str, d: dict) -> dict:
    # After he confirms, what he hears is the save's own result (its first run as of then), not the draft's.
    return {"tool": f"{NAME}.save", "args": {"draft": did}, "reason": _readback(d),
            **({} if d["type"] == "automation" else {"reply": f"Saved. Say \"{d['phrases'][0]}\" to run it."})}


async def automate_tool(app, user: str, args: dict) -> dict:
    d = await parse(app, str(args.get("say") or ""), user)
    _readback(d)
    did = await _draft(app, d)
    return {"headline": "Save this automation?", "automation_draft": {k: d[k] for k in ("readback", "first", "notes")},
            "confirm": _confirm(did, d)}


async def routine_tool(app, user: str, args: dict) -> dict:
    d = await parse_routine(app, str(args.get("say") or ""), user)
    _readback(d)
    did = await _draft(app, d)
    return {"headline": f"Save the routine \"{d['name']}\"?",
            "routine_draft": {"name": d["name"], "steps": [{"n": s["n"], "title": s["title"], "after": bool(s["depends"])}
                                                           for s in d["steps"]],
                              "phrases": d["phrases"][:2], "notes": d["notes"]},
            "confirm": _confirm(did, d)}


async def save_tool(app, user: str, args: dict) -> dict:
    """Runs only from his confirmation (change tier, never trusted): the draft exactly as he saw it read back."""
    raw = await app.state.redis.getdel(DRAFT.format(str(args.get("draft") or "")))
    if raw is None:
        raise ValueError("That draft expired or was already saved. Say it again and I'll read it back.")
    d = json.loads(raw)
    if d.get("user") != user:
        raise ValueError("That draft is someone else's.")
    if d["type"] == "routine":
        return await _save_routine(app, d)
    return await _save_automation(app, d, user, await _asker(args))


async def _asker(args: dict) -> int | None:
    tid = str(args.get("_self") or "")
    if not tid:
        return None
    return await db.pool().fetchval("SELECT device FROM task_runs WHERE id=$1", uuid.UUID(tid))


async def _save_automation(app, d: dict, user: str, device: int | None, conn=None) -> dict:
    from . import engine
    same = await (conn or db.pool()).fetchval(
        "SELECT id FROM jobs WHERE created_by='owner' AND user_id=$1 AND kind=$2 AND spec=$3::jsonb AND rule=$4::jsonb "
        "AND status NOT IN ('cancelled','done','failed','missed') LIMIT 1",
        user, d["kind"], json.dumps(d["spec"]), json.dumps(d["rule"]))
    if same:
        raise ValueError("You already have exactly that automation. Say \"show my automations\" to see it.")
    job = {"kind": d["kind"], "spec": d["spec"], "rule": d["rule"], "tz": engine.tz_name(app),
           "heads_up": engine.heads_for(app, d["kind"], d["spec"]), "created_by": "owner"}
    _, planned, _, _ = engine.slot(app, job, engine.now())
    row = await engine.add(app, d["kind"], d["title"], d["spec"], planned, user, device, d["rule"], conn=conn)
    v = engine.view(row, engine.now())
    nxt = "" if d["rule"].get("on") else f" First run: {v['when']}."
    return {"headline": f"Automation saved: {d['readback'].rstrip('.')}", "automation": {"job": v, "note": nxt.strip()},
            "did": {"node": "Pulsar", "what": f"Saved: {d['readback']}{nxt}"}}


async def _save_routine(app, d: dict) -> dict:
    import asyncpg
    from . import admiral, morpho
    steps = admiral.validate({"steps": d["steps"]}, await admiral.catalog(app))
    try:
        async with db.pool().acquire() as conn, conn.transaction():
            sid = await conn.fetchval(
                "INSERT INTO skills (name, version, slots, steps, report, phrases, source, status, approved_at, note) "
                "VALUES ($1, 1, '[]'::jsonb, $2::jsonb, '', $3::jsonb, 'routine', 'active', now(), $4) RETURNING id",
                d["name"], json.dumps(steps), json.dumps(d["phrases"]), "Made from your words (a routine).")
    except asyncpg.UniqueViolationError:
        raise ValueError(f"You already have a routine or skill called \"{d['name']}\". Pick another name.")
    sk = morpho._skill(await db.pool().fetchrow("SELECT * FROM skills WHERE id=$1", sid))
    return {"headline": f"Routine saved: {d['name']}", "skills": {"items": [{
        "id": sid, "name": sk["name"], "version": 1, "status": "active", "slots": [], "phrases": sk["phrases"][:2],
        "uses": 0, "steps": len(sk["steps"]), "model_free": True}]},
        "did": {"node": "Pulsar", "what": f"Saved the routine \"{d['name']}\". Say \"{d['phrases'][0]}\" to run it."}}


# ---- his automations: list, pause, resume, skip, delete -------------------------------------------------------

LIVE = ("scheduled", "armed", "paused", "waiting", "running")


async def _mine(user: str) -> list[dict]:
    return [dict(r) for r in await db.pool().fetch(
        "SELECT * FROM jobs WHERE created_by='owner' AND user_id=$1 AND NOT (rule ? 'once') AND status = ANY($2::text[]) "
        "ORDER BY status = 'paused', next_at", user, list(LIVE))]


async def find(user: str, which: str, statuses: tuple[str, ...] = LIVE) -> dict:
    from . import engine
    rows = [j for j in await _mine(user) if j["status"] in statuses]
    if not rows:
        raise ValueError("You don't have any automations" + ("" if statuses == LIVE else " like that") + ".")
    words = [w for w in re.findall(r"[a-z0-9:']+", when._clean(which)) if w not in _STOP]
    hits = [j for j in rows if all(w in engine._hay(j) for w in words)] if words else (rows if len(rows) == 1 else [])
    if not hits:
        raise ValueError("None of your automations matches that. Say \"show my automations\" to see them.")
    if len(hits) > 1:
        raise ValueError("Which one? " + "; ".join(f"{j['title']} ({engine.repeats(j)})" for j in hits[:5]))
    return hits[0]


async def automations_tool(app, user: str, args: dict) -> dict:
    from . import engine, morpho
    n = engine.now()
    items = []
    for j in await _mine(user):
        v = engine.view(j, n)
        runs = await db.pool().fetch("SELECT outcome, started_at, note FROM job_runs WHERE job_id=$1 "
                                     "ORDER BY id DESC LIMIT 3", j["id"])
        z = ZoneInfo(j["tz"])
        v["last"] = [{"outcome": r["outcome"], "when": when.say(r["started_at"].astimezone(z), n),
                      **({"note": r["note"]} if r["note"] else {})} for r in runs]
        if j["fails_in_row"]:
            v["fails_in_row"] = j["fails_in_row"]
        items.append(v)
    routines = [{"id": s["id"], "name": s["name"], "steps": [x["title"] for x in s["steps"]], "phrases": s["phrases"][:2],
                 "uses": s["uses"]} for s in await morpho.active_skills() if s.get("source") == "routine"]
    paused = sum(1 for i in items if i["status"] == "paused")
    head = (f"{len(items)} automation{'s' if len(items) != 1 else ''}" + (f" ({paused} paused)" if paused else "")
            + (f" · {len(routines)} routine{'s' if len(routines) != 1 else ''}" if routines else "")) \
        if items or routines else "No automations yet"
    return {"headline": head, "automations": {"items": items, "routines": routines}}


async def pause_tool(app, user: str, args: dict) -> dict:
    from . import engine
    j = await find(user, str(args.get("which") or ""), ("scheduled", "armed", "waiting"))
    if await engine.pause(app, j["id"], "you paused it") is None:
        raise ValueError("It's running right now; try again in a moment.")
    return {"headline": f"Paused: {j['title']}", "did": {"node": "Pulsar", "what": f"Paused: {j['title']} "
                                                                                  f"({engine.repeats(j)}). Say \"resume\" to turn it back on."}}


async def resume_tool(app, user: str, args: dict) -> dict:
    from . import engine
    j = await find(user, str(args.get("which") or ""), ("paused",))
    out = await engine.resume(app, j["id"])
    if out is None:
        raise ValueError("It isn't paused anymore.")
    return {"headline": f"Resumed: {j['title']}", "did": {"node": "Pulsar", "what": f"Resumed: {j['title']}. {out['next']}"}}


async def skip_tool(app, user: str, args: dict) -> dict:
    from . import engine
    j = await find(user, str(args.get("which") or ""), ("scheduled", "waiting"))
    out = await engine.skip(app, j["id"], "you skipped it")
    return {"headline": f"Skipping the next one: {j['title']}", "did": {"node": "Pulsar", "what": f"Skipped once: "
                                                                                                f"{j['title']}. {out['skipped']}"}}


async def delete(app, job_id: int, why: str = "you deleted it") -> dict | None:
    """Gone for good. One that's running right now finishes this run and doesn't run again."""
    from . import engine, mailbox
    running = await db.pool().fetchrow(
        "UPDATE jobs SET spec = spec || '{\"deleted\": true}'::jsonb, updated_at=now() WHERE id=$1 AND created_by='owner' "
        "AND NOT (rule ? 'once') AND status='running' RETURNING *", job_id)
    if running is not None:
        return {**dict(running), "last_run": True}
    row = await db.pool().fetchrow(
        "UPDATE jobs SET status='cancelled', last_outcome='deleted', lease_until=NULL, updated_at=now() WHERE id=$1 "
        "AND created_by='owner' AND NOT (rule ? 'once') AND status IN ('scheduled','armed','paused','waiting') RETURNING *",
        job_id)
    if row is None:
        return None
    if row["mailbox_id"] and await db.pool().fetchval(
            "UPDATE mailbox_items SET status='expired', updated_at=now() WHERE id=$1 AND status IN ('waiting','deferred') "
            "RETURNING id", row["mailbox_id"]):
        await mailbox.notify(app.state.redis)
    await engine._close_heads_up(app, job_id)
    await engine._log(job_id, engine._planned(dict(row)), "deleted", None, why)
    return dict(row)


async def delete_tool(app, user: str, args: dict) -> dict:
    from . import engine
    j = await find(user, str(args.get("which") or ""), ("scheduled", "armed", "paused", "waiting", "running"))
    out = await delete(app, j["id"])
    if out is None:
        raise ValueError("It changed just now; try again.")
    last = " It's running right now: this run is its last." if out.get("last_run") else ""
    return {"headline": f"Deleted: {j['title']}", "did": {"node": "Pulsar", "what": f"Deleted the automation: {j['title']} "
                                                                                   f"({engine.repeats(j)}).{last}"}}


async def forget_routine_tool(app, user: str, args: dict) -> dict:
    which = re.sub(r"^(?:the|my)\s+|\s+routine$", "", _clean(str(args.get("which") or "")), flags=re.I).strip()
    row = await db.pool().fetchrow("SELECT * FROM skills WHERE status='active' AND source='routine' AND lower(name)=lower($1)",
                                   which)
    if row is None:
        raise ValueError(f"I don't have a routine called \"{which}\". Say \"show my automations\" to see your routines.")
    await db.pool().execute("UPDATE skills SET status='retired' WHERE id=$1 AND status='active'", row["id"])
    gone = []
    for j in await db.pool().fetch("SELECT id, title FROM jobs WHERE kind='routine' AND (spec->>'skill_id')::bigint=$1 "
                                   "AND created_by='owner' AND NOT (rule ? 'once') "
                                   "AND status IN ('scheduled','armed','paused','waiting','running')", row["id"]):
        if await delete(app, j["id"], "its routine was deleted"):
            gone.append(j["title"])
    more = f" Also deleted {len(gone)} automation{'s' if len(gone) != 1 else ''} that ran it." if gone else ""
    return {"headline": f"Deleted the routine: {row['name']}", "did": {"node": "Pulsar", "what": f"Deleted the routine "
                                                                                              f"\"{row['name']}\".{more}"}}


# ---- automations Parallax suggested (approved in the mailbox) -------------------------------------------------

async def _from_card(app, conn, item_id: int | None, p: dict) -> tuple[str, int | None]:
    """A habit card's automation (his days and time), made like one he'd said. (what he's told, the job)."""
    from . import admiral
    tool = str(p["tool"])
    if tool.startswith(NOT_AUTOMATED) or admiral.tier_of(app, tool) == "never":
        raise HTTPException(400, "That can't be automated.")
    title = (await conn.fetchval("SELECT title FROM mailbox_items WHERE id=$1", item_id) if item_id else None) or \
        tool.split(".", 1)[1].replace("_", " ")
    title = re.sub(r"\s+every\s+(?:weekday|day)\s+at\s+\d{1,2}:\d{2}\s*[AP]M$", "", title).strip() or title
    rule = {"days": p["schedule"]["days"], "time": p["schedule"]["time"]}
    said = when.say_rule(rule)
    d = {"kind": "tool", "title": _sentence(title), "spec": {"tool": tool, "args": p.get("args") or {}}, "rule": rule,
         "readback": f"{said[:1].upper() + said[1:]}: {_sentence(title)}."}
    try:
        out = await _save_automation(app, d, "owner", None, conn=conn)
    except ValueError as exc:
        return str(exc), None
    extra = " It asks for your OK each time." if admiral.tier_of(app, tool) == "change" else ""
    return f"Saved: {d['readback']} {out['automation']['note']}".strip() + extra, out["automation"]["job"]["id"]


async def from_suggestion(app, conn, item_id: int, p: dict) -> str:
    """His OK on a Parallax habit card (mailbox): the automation it describes, made now, with his changes to the
    days or time. Checked like one he'd said: never-tier refused, a change-tier action asks him each time."""
    said, job_id = await _from_card(app, conn, item_id, p)
    await conn.execute("INSERT INTO automations (tool, args, schedule, from_item, status, job_id) "
                       "VALUES ($1, $2::jsonb, $3::jsonb, $4, $5, $6)", str(p["tool"]), json.dumps(p.get("args") or {}),
                       json.dumps(p["schedule"]), item_id, "active" if job_id else "refused", job_id)
    return said


async def adopt_waiting(app) -> int:
    """Automations he approved before Pulsar existed ('it runs once Pulsar is built'): each becomes one now, and
    he's told which (once). One row at a time, claimed and made in one transaction: two orchestrators never make
    one twice, and a restart halfway leaves the row for the next pass."""
    made = []
    for _ in range(200):
        r = None
        async with db.pool().acquire() as conn:
            try:
                async with conn.transaction():
                    r = await conn.fetchrow("SELECT * FROM automations WHERE status='waiting_for_pulsar' ORDER BY id "
                                            "LIMIT 1 FOR UPDATE SKIP LOCKED")
                    if r is None:
                        break
                    p = {"tool": r["tool"],
                         "args": json.loads(r["args"]) if isinstance(r["args"], str) else (r["args"] or {}),
                         "schedule": json.loads(r["schedule"]) if isinstance(r["schedule"], str) else r["schedule"]}
                    said, job_id = await _from_card(app, conn, r["from_item"], p)
                    await conn.execute("UPDATE automations SET status=$2, job_id=$3 WHERE id=$1", r["id"],
                                       "active" if job_id else "refused", job_id)
                if job_id:
                    made.append(said)
            except Exception as exc:
                log.warning("couldn't adopt automation %s: %s", r["id"] if r else "?", exc)
                if r is not None:
                    await conn.execute("UPDATE automations SET status='refused' WHERE id=$1 AND status='waiting_for_pulsar'",
                                       r["id"])
    if made:                                     # a key of its own: a later batch is told too
        await notice(app, "automations_ready", f"adopted:{uuid.uuid4().hex[:8]}",
                     f"{len(made)} automation{'s' if len(made) != 1 else ''} you approved earlier now run",
                     " ".join(made)[:900] + " Say \"show my automations\" to see or pause them.")
    return len(made)


# ---- notices: what's worth telling him, and how --------------------------------------------------------------

async def notice(app, kind: str, key: str, title: str, text: str = "") -> dict | None:
    """Something worth knowing. Raised once per kind and key while it lasts: its severity (his governance)
    decides how he hears; repeats only after his repeat interval, and never once he's said "got it"."""
    import asyncpg
    sev = level(app, kind)
    row = await db.pool().fetchrow(
        "UPDATE notices SET last_at=now(), count=count+1, title=$3, text=$4 WHERE kind=$1 AND key=$2 AND status='open' "
        "RETURNING *", kind, key, title[:200], text[:1000])
    if row is not None:
        rep = float(rules(app)["repeat_hours"].get(row["severity"]) or 0)
        if (not row["seen"] and not row["held"] and rep > 0 and row["told_at"] is not None
                and datetime.now(timezone.utc) - row["told_at"] >= timedelta(hours=rep)):
            await _tell(app, dict(row), again=True)
        return dict(row)
    try:
        row = await db.pool().fetchrow("INSERT INTO notices (kind, key, severity, title, text) VALUES ($1, $2, $3, $4, $5) "
                                       "RETURNING *", kind, key, sev, title[:200], text[:1000])
    except asyncpg.UniqueViolationError:                 # raised at the same moment elsewhere: that one tells him
        return None
    await _tell(app, dict(row))
    return dict(row)


async def clear(app, kind: str, key: str) -> bool:
    """It went away (the door was locked, the computer came back): closed, and its card taken back."""
    from . import mailbox
    row = await db.pool().fetchrow("UPDATE notices SET status='cleared', cleared_at=now(), held=false WHERE kind=$1 "
                                   "AND key=$2 AND status='open' RETURNING *", kind, key)
    if row is None:
        return False
    if row["mailbox_id"] and await db.pool().fetchval(
            "UPDATE mailbox_items SET status='expired', updated_at=now() WHERE id=$1 AND status IN ('waiting','deferred') "
            "RETURNING id", row["mailbox_id"]):
        await mailbox.notify(app.state.redis)
    return True


def _link(n: dict) -> str:
    return f"/view?notice={n['id']}"


async def _tell(app, n: dict, again: bool = False) -> None:
    """By severity: critical everywhere at once (phone, every open display, an urgent card), even in quiet hours;
    high on his phone now, or when quiet hours end; normal a card (no phone); low the digest only."""
    from . import mailbox, push
    from .events import PanelEvent, publish
    sev = n["severity"]
    quiet = is_quiet(app)
    if sev == "low":
        await db.pool().execute("UPDATE notices SET told_at=now() WHERE id=$1", n["id"])
        return
    card = n.get("mailbox_id")
    if not again and n["kind"] != "mailbox_waiting":         # (a card about the mailbox would sit in the mailbox)
        try:
            out = await mailbox.propose_internal(app.state.redis, mailbox.Proposal(
                kind="notice", fingerprint=f"notice:{n['id']}", source=NAME, title=n["title"], summary=n["text"],
                proposal={"notice_id": n["id"]}, evidence=[f"Severity: {sev} (yours to change in pulsar.yaml)"],
                confidence=1, urgent=sev == "critical" or (sev == "high" and not quiet),
                effect="Nothing changes: it tells me you've seen it, so I don't remind you again unless it clears "
                       "and comes back."))
            card = out["id"]
        except Exception:
            log.exception("notice card")
    held = sev == "high" and quiet
    await db.pool().execute("UPDATE notices SET told_at=now(), mailbox_id=$2, held=$3 WHERE id=$1", n["id"], card, held)
    if sev == "critical":                                    # on every display that's open, whatever it's showing
        tid = f"notice-{n['id']}-{n.get('count', 1)}"
        data = {"headline": n["title"], "notice": {"id": n["id"], "text": n["text"], "severity": sev}}
        await publish(app.state.redis, PanelEvent(type="panel.open", task_id=tid, tool=f"{NAME}.notice",
                                                  data={"tier": "reference", "args": {}}))
        await publish(app.state.redis, PanelEvent(type="panel.result", task_id=tid, tool=f"{NAME}.notice",
                                                  data={"status": "completed", "partial": False, **data}))
    # A new urgent card reaches his phone through the mailbox's own notifications; a repeat comes from here.
    if again and not held and sev in ("critical", "high"):
        try:
            await push.notify(app, n["title"], n["text"][:250], _link(n), f"notice-{n['id']}",
                              urgency="high" if sev == "critical" else "normal")
        except Exception:
            log.exception("notice push")
    if not held:                                             # a second path to his phone (his ntfy), by severity:
        from . import beacon_glue                            # last, so ntfy never delays the displays or the push
        await beacon_glue.second_path(app, n)


async def release_held(app) -> int:
    """Quiet hours are over: what was held for them reaches his phone now."""
    from . import push
    if is_quiet(app):
        return 0
    rows = await db.pool().fetch("UPDATE notices SET held=false, told_at=now() WHERE held AND status='open' RETURNING *")
    from . import beacon_glue
    for n in rows:
        try:                                     # (its card stays as it was: the mailbox's own push would repeat this)
            await push.notify(app, n["title"], n["text"][:250], _link(dict(n)), f"notice-{n['id']}")
        except Exception:
            log.exception("held notice")
    if rows:                                     # every push first; then ntfy, all at once (it never raises)
        await asyncio.gather(*(beacon_glue.second_path(app, dict(n)) for n in rows))
    return len(rows)


async def seen(notice_id: int) -> str:
    await db.pool().execute("UPDATE notices SET seen=true, held=false WHERE id=$1", notice_id)
    return "Got it. I won't remind you again unless it clears and comes back."


def _view(n: dict, at: datetime, tz: str) -> dict:
    z = ZoneInfo(tz)
    return {"id": n["id"], "kind": n["kind"], "title": n["title"], "text": n["text"], "severity": n["severity"],
            "since": when.say(n["first_at"].astimezone(z), at), "status": n["status"],
            **({"cleared": when.say(n["cleared_at"].astimezone(z), at)} if n.get("cleared_at") else {}),
            **({"seen": True} if n["seen"] else {}), **({"held": True} if n["held"] else {}),
            **({"count": n["count"]} if n["count"] > 1 else {})}


async def notices_tool(app, user: str, args: dict) -> dict:
    from . import engine
    n, tz = engine.now(), engine.tz_name(app)
    rank = {lv: i for i, lv in enumerate(LEVELS)}
    open_ = sorted([dict(r) for r in await db.pool().fetch("SELECT * FROM notices WHERE status='open'")],
                   key=lambda x: (rank.get(x["severity"], 9), x["first_at"]))
    recent = [dict(r) for r in await db.pool().fetch(
        "SELECT * FROM notices WHERE status='cleared' AND cleared_at > now() - interval '1 day' ORDER BY cleared_at DESC LIMIT 10")]
    checks = {k.decode() if isinstance(k, bytes) else k: (v.decode() if isinstance(v, bytes) else v)
              for k, v in (await app.state.redis.hgetall(CHECKS) or {}).items()}
    watching = [{"kind": k, "what": what, "level": level(app, k), "check": checks.get(k, "not checked yet")}
                for k, what in WATCHING.items()]
    head = (f"{len(open_)} open" + (f" · {sum(1 for x in open_ if x['severity'] == 'critical')} critical"
                                    if any(x["severity"] == "critical" for x in open_) else "")) if open_ else "All clear"
    return {"headline": head, "notices": {"open": [_view(x, n, tz) for x in open_], "recent": [_view(x, n, tz) for x in recent],
                                          "watching": watching, "quiet_hours": quiet_spec(app) or "none"}}


@router.get("/notices/{notice_id}/panel")
async def notice_panel(notice_id: int, request: Request):
    """What a notice's notification opens."""
    from . import engine
    row = await db.pool().fetchrow("SELECT * FROM notices WHERE id=$1", notice_id)
    if row is None:
        raise HTTPException(404, "No such notice.")
    v = _view(dict(row), engine.now(), engine.tz_name(request.app))
    return {"task_id": f"notice-{notice_id}", "tool": f"{NAME}.notice",
            "data": {"status": "completed", "partial": False, "headline": row["title"], "notice": v}}


# ---- watching: events, and what's worth telling him ------------------------------------------------------------

async def _check(app, kind: str, state: str) -> None:
    try:
        await app.state.redis.hset(CHECKS, kind, state[:200])
    except Exception:
        pass


async def _every(app, name: str, minutes: float) -> bool:
    """True at most once per `minutes` (across orchestrators)."""
    return bool(await app.state.redis.set(THROTTLE.format(name), "1", nx=True, ex=max(1, int(minutes * 60))))


async def events_step(app, spec: dict) -> dict | None:
    """A look at what I can see happen: computers coming online or going offline, displays opening. An armed
    automation waiting for that runs (after its heads-up), at most once per cooldown."""
    from . import engine, happenings, nodes
    from .constellation import DISPLAY_SEEN
    redis = app.state.redis
    seen_now: dict[str, str] = {}
    for r in await db.pool().fetch("SELECT id FROM devices WHERE kind='node' AND status='approved'"):
        seen_now[f"node:{r['id']}"] = "1" if await nodes.online(app, r["id"]) else "0"
    for r in await db.pool().fetch("SELECT id FROM devices WHERE kind IN ('browser','screen') AND status='approved'"):
        seen_now[f"display:{r['id']}"] = "1" if await redis.exists(DISPLAY_SEEN.format(r["id"])) else "0"
    before = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
              for k, v in (await redis.hgetall(SNAP) or {}).items()}
    if seen_now:
        await redis.hset(SNAP, mapping=seen_now)
    happened = []
    for k, v in seen_now.items():
        what, _, dev = k.partition(":")
        if what == "node":                                   # how long each computer has been away (for computer_down)
            if v == "0" and not await redis.hexists(OFFLINE_SINCE, dev):
                await redis.hset(OFFLINE_SINCE, dev, str(engine.now().timestamp()))
            elif v == "1":
                await redis.hdel(OFFLINE_SINCE, dev)
        was = before.get(k)
        if was is None or was == v:
            continue                                         # first look, or no change
        if what == "node":
            happened.append(("online" if v == "1" else "offline", int(dev)))
        elif v == "1":
            happened.append(("open", int(dev)))
    cool = float(rules(app)["event_cooldown_minutes"]) * 60
    fired = 0
    for ev, dev in happened:
        for j in await db.pool().fetch("SELECT id, title, rule, last_run_at FROM jobs WHERE status='armed' AND rule->>'on'=$1 "
                                       "AND rule->>'device'=$2", ev, str(dev)):
            if j["last_run_at"] and (engine.now() - j["last_run_at"]).total_seconds() < cool:
                continue
            if not happenings.allowed(app, engine._j(j["rule"])):      # its time window, days, quiet hours
                continue
            fired += await engine.fire(app, j["id"], f"{ev} {dev}")
    return {"note": f"{len(happened)} event(s), {fired} automation(s) started"} if happened else None


async def watch_step(app, spec: dict) -> dict | None:
    """Each minute: release what quiet hours held, and look at what he'd want to know about."""
    await release_held(app)
    for name, fn, every in (("adopt", adopt_waiting, 60), ("server_disk_low", _watch_disk, 1),
                            ("computer_down", _watch_computers, 1), ("update_failed", _watch_releases, 1),
                            ("home", _watch_home, float(rules(app)["watch"]["home_check_minutes"])),
                            ("mailbox_waiting", _watch_mailbox, 30), ("budget_ahead", _watch_budget, 60)):
        try:
            if every <= 1 or await _every(app, name, every):
                await fn(app)
        except Exception as exc:
            log.exception("pulsar watcher %s", name)
            await _check(app, name, f"couldn't check: {exc}")
    await db.pool().execute("UPDATE notices SET status='cleared', cleared_at=now(), held=false WHERE status='open' "
                            "AND kind = ANY($1::text[]) AND first_at < now() - interval '1 day'", list(ONE_SHOT))
    return None


async def _watch_disk(app) -> None:
    pct = float(rules(app)["watch"]["server_disk_free_percent"])
    du = shutil.disk_usage("/")
    free = du.free / du.total * 100 if du.total else 100
    await _check(app, "server_disk_low", f"ok ({free:.0f}% free)")
    if free < pct:
        await notice(app, "server_disk_low", "server", f"The server is running out of disk ({free:.0f}% free)",
                     f"{du.free / 1e9:.1f} GB of {du.total / 1e9:.0f} GB is free. When it fills, I can't save anything, "
                     "and updates fail. Old Docker images and logs are the usual place to make room.")
    elif free > pct + 2:
        await clear(app, "server_disk_low", "server")


async def _watch_computers(app) -> None:
    from . import engine, nodes
    w = rules(app)["watch"]
    always = {str(x).lower() for x in w.get("always_on") or []}
    mins = float(w["computer_down_minutes"])
    since = {(k.decode() if isinstance(k, bytes) else k): float(v) for k, v in
             (await app.state.redis.hgetall(OFFLINE_SINCE) or {}).items()}
    watched = 0
    for r in await db.pool().fetch("SELECT id, kind, name, role, info FROM devices WHERE kind='node' AND status='approved'"):
        role = await auth.role_of(r)
        if role != "server" and r["name"].lower() not in always:
            continue
        watched += 1
        key = f"node:{r['id']}"
        if await nodes.online(app, r["id"]):
            await clear(app, "computer_down", key)
            continue
        info = json.loads(r["info"]) if isinstance(r["info"], str) else (r["info"] or {})
        if info.get("mode") == "exam" and float(info.get("mode_until") or 0) > engine.now().timestamp():
            continue
        if await db.pool().fetchval(                       # he turned it off (or put it to sleep) himself
                "SELECT 1 FROM jobs WHERE name='constellation.power_watch' AND (spec->'node'->>'id')::int=$1 "
                "AND spec->>'kind' IN ('shutdown','sleep') AND created_at > now() - interval '12 hours'", r["id"]):
            continue
        gone = since.get(str(r["id"]))
        if gone is not None and engine.now().timestamp() - gone >= mins * 60:
            await notice(app, "computer_down", key, f"{r['name']} is down",
                         f"{r['name']} stopped checking in {int((engine.now().timestamp() - gone) // 60)} minutes ago. "
                         f"Ask me \"what's wrong with {r['name']}\" once it's back, or check its power and network.")
    await _check(app, "computer_down", f"ok (watching {watched} computer{'s' if watched != 1 else ''})" if watched else
                 "nothing to watch: no computer has the server role (or is listed in watch.always_on)")


async def _watch_releases(app) -> None:
    rows = await db.pool().fetch("SELECT id, sha, message, status, detail FROM releases WHERE status IN ('failed','rolled_back') "
                                 "AND started_at > now() - interval '1 day'")
    for r in rows:
        if await db.pool().fetchval("SELECT 1 FROM notices WHERE kind='update_failed' AND key=$1", f"release:{r['id']}"):
            continue
        msg = (r["message"] or "").splitlines()[0] if r["message"] else r["sha"][:8]
        await notice(app, "update_failed", f"release:{r['id']}",
                     f"An update {'was rolled back' if r['status'] == 'rolled_back' else 'failed'}: {msg}"[:200],
                     (r["detail"] or "")[:600] + " Say \"show my releases\" for the details.")
    await _check(app, "update_failed", "ok")


async def _watch_mailbox(app) -> None:
    hours = float(rules(app)["watch"]["mailbox_waiting_hours"])
    n = await db.pool().fetchval("SELECT count(*) FROM mailbox_items WHERE status='waiting' AND source <> $1 "
                                 "AND created_at < now() - make_interval(secs => $2)", NAME, hours * 3600)
    await _check(app, "mailbox_waiting", "ok")
    if n:
        await notice(app, "mailbox_waiting", "mailbox", f"{n} thing{'s' if n != 1 else ''} waiting in your mailbox",
                     f"{'They have' if n != 1 else 'It has'} been waiting more than {hours:g} hours.")
    else:
        await clear(app, "mailbox_waiting", "mailbox")


async def _watch_budget(app) -> None:
    from . import budget
    b = (await budget.budget(app, "owner", {}))["budget"]
    key = f"month:{b['month']}"
    await _check(app, "budget_ahead", f"ok ({b['percent']:g}% used)")
    for old in await db.pool().fetch("SELECT key FROM notices WHERE kind='budget_ahead' AND status='open' AND key <> $1", key):
        await clear(app, "budget_ahead", old["key"])            # a new month: last month's is over
    if b["state"] == "reached":
        await notice(app, "budget_ahead", key, "This month's model budget is used up",
                     f"I'm not calling Claude until it resets {b['resets_in']} ({b['resets']}). Raising "
                     "llm.monthly_token_cap takes effect right away.")
    elif b.get("hits_cap"):
        await notice(app, "budget_ahead", key, f"At this pace, the model budget runs out around {b['hits_cap']}",
                     f"{b['percent']:g}% used; about {b['projected_text']} by the month's end against a cap of {b['cap_text']}.")
    else:
        await clear(app, "budget_ahead", key)


async def _watch_home(app) -> None:
    """The home's own state (Orbit's status, as Pulsar's system user, which only looks): doors left unlocked
    during quiet hours, a wet leak sensor, low batteries."""
    from fastapi import HTTPException as HTTPExc
    from .tasks import StartTask, launch, wait_for_result
    if await registry.resolve(app.state.redis, "orbit.status") is None:
        await _check(app, "door_unlocked_at_night", "can't check: Orbit isn't running")
        return
    try:
        tid = await launch(app, StartTask(tool="orbit.status", args={}, user=SYSTEM_USER, trigger=NAME, display="brief"))
    except HTTPExc as exc:
        d = exc.detail if isinstance(exc.detail, dict) else {}
        why = str(d.get("reason") or exc.detail)
        why = ("add the pulsar user to charter.yaml" if "unknown user" in why else why)
        for k in ("door_unlocked_at_night", "water_leak", "battery_low"):
            await _check(app, k, f"can't check: {why}")
        return
    res = await wait_for_result(tid, 60)
    if not res or res["status"] != "completed":
        why = (res or {}).get("error") or "it didn't answer"
        for k in ("door_unlocked_at_night", "water_leak", "battery_low"):
            await _check(app, k, f"can't check: {why}"[:200])
        return
    devices = (res.get("result") or {}).get("devices") or []
    w = rules(app)["watch"]
    quiet = is_quiet(app)
    from . import engine
    local = datetime.now(ZoneInfo(engine.tz_name(app)))
    for d in devices:
        name = str(d.get("name") or "a device")
        if d.get("kind") == "lock" and d.get("locked") is False and quiet:
            await notice(app, "door_unlocked_at_night", name, f"{name} is unlocked",
                         f"It's {local.strftime('%I:%M %p').lstrip('0')} and {name} is still unlocked. Say \"lock the "
                         f"{name.lower()}\" to lock it.")
        elif d.get("kind") == "lock" and (d.get("locked") is True or not quiet):
            await clear(app, "door_unlocked_at_night", name)
        if d.get("kind") == "leak_sensor":
            if d.get("active") is True:
                await notice(app, "water_leak", name, f"Water: {name} is wet", f"{name} detects water. Check it now.")
            else:
                await clear(app, "water_leak", name)
        bat = d.get("battery")
        if isinstance(bat, (int, float)):
            if bat <= float(w["battery_low_percent"]):
                await notice(app, "battery_low", name, f"{name}: battery at {bat:g}%", "Replace it soon.")
            elif bat > float(w["battery_low_percent"]) + 5:
                await clear(app, "battery_low", name)
    locks = sum(1 for d in devices if d.get("kind") == "lock")
    await _check(app, "door_unlocked_at_night", f"ok ({locks} lock{'s' if locks != 1 else ''}" +
                 (", checked now: quiet hours)" if quiet else "; only flagged during quiet hours)"))
    await _check(app, "water_leak", f"ok ({sum(1 for d in devices if d.get('kind') == 'leak_sensor')} sensor(s))")
    await _check(app, "battery_low", "ok")


def _register_jobs() -> None:
    from . import engine
    engine.system("pulsar.events", every=lambda app: 0.5)(events_step)
    engine.system("pulsar.watch", every=lambda app: 1)(watch_step)


_register_jobs()


# ---- buttons on its panels (a TV's screen can't press these) -------------------------------------------------

async def _from_display(request: Request) -> None:
    dev = await auth.asker(request.headers)
    if dev is not None and dev["kind"] == "screen":
        raise HTTPException(403, "A TV's screen can't change automations.")


@router.post("/automations/{job_id}/{action}")
async def automation_http(job_id: int, action: str, request: Request):
    from . import engine
    await _from_display(request)
    app = request.app
    try:
        if action == "pause":
            row = await engine.pause(app, job_id, "you paused it")
            if row is None:
                raise ValueError("That can't be paused right now.")
            return {"ok": True, "done": f"Paused: {row['title']}."}
        if action == "resume":
            row = await engine.resume(app, job_id)
            if row is None:
                raise ValueError("It isn't paused.")
            return {"ok": True, "done": f"Resumed: {row['title']}. {row['next']}"}
        if action == "skip":
            row = await engine.skip(app, job_id, "you skipped it")
            return {"ok": True, "done": f"Skipped once: {row['title']}. {row['skipped']}"}
        if action == "delete":
            row = await delete(app, job_id)
            if row is None:
                raise ValueError("That can't be deleted right now.")
            return {"ok": True, "done": f"Deleted: {row['title']}." + (" This run is its last." if row.get("last_run") else "")}
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    raise HTTPException(404, "No such action.")


# ---- registration and the function contract (like Admiral's) --------------------------------------------------

HANDLERS = {"automate": automate_tool, "save": save_tool, "automations": automations_tool, "pause": pause_tool,
            "resume": resume_tool, "skip": skip_tool, "delete": delete_tool, "routine": routine_tool,
            "forget_routine": forget_routine_tool, "notices": notices_tool}


async def register_loop(app) -> None:
    from .settings import settings
    while True:
        ttl = app.state.config.current.registry.function_ttl_seconds
        try:
            await registry.register(app.state.redis, registry.FunctionRegistration(
                name=NAME, description="Automations and routines in his words, and what's worth interrupting him for.",
                url=f"{settings.self_url.rstrip('/')}/pulsar", tools=TOOLS), ttl)
        except Exception:
            log.exception("couldn't register Pulsar; will retry")
        await asyncio.sleep(max(3, ttl // 3))


@router.get("/health")
async def health():
    return {"status": "ok"}


class Invoke(BaseModel):
    task_id: str
    args: dict = {}


@router.post("/tools/{tool}/invoke", status_code=202)
async def invoke(tool: str, body: Invoke, request: Request):
    if tool not in HANDLERS:
        raise HTTPException(404, f"pulsar has no tool '{tool}'")
    asyncio.create_task(_run(request.app, tool, body.task_id, body.args))
    return {}


@router.post("/cancel/{task_id}", status_code=202)
async def cancel(task_id: str):
    return {}


async def _run(app, tool: str, task_id: str, args: dict) -> None:
    from . import tasks
    try:
        row = await db.pool().fetchrow("SELECT user_id FROM task_runs WHERE id=$1", uuid.UUID(task_id))
        data = await HANDLERS[tool](app, row["user_id"] if row else "owner", {**args, "_self": task_id})
        result = tasks.Result(status="completed", data=data)
    except ValueError as exc:
        result = tasks.Result(status="failed", error=str(exc))
    except Exception as exc:
        log.exception("pulsar.%s failed", tool)
        result = tasks.Result(status="failed", error=f"Something went wrong: {exc}")
    await tasks._finish(tasks._AppContext(app), task_id, f"{NAME}.{tool}", result)
