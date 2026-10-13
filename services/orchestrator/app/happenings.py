"""What happens in the home, as events an automation can wait for (Pulsar, with Lagrange and Orbit).

- A device or entity changing state: a lock unlocking, a light turning on, a door opening, a leak sensor
  getting wet, motion, a person coming home or leaving. Orbit reports its SmartRent devices' live changes;
  Lagrange reports Home Assistant's. Each is checked against the last state I heard (atomically, so two
  copies of me, or the same change reported twice, start nothing twice).
- A webhook in being called (Lagrange): "when the arrive home webhook is called".

His words: "when the front door unlocks during quiet hours, turn on the hall light", "turn on the porch light
when I get home", "when the garage door opens, …", "when the deploy webhook is called, …".
"""
import json
import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request

from . import db, registry
from . import net

log = logging.getLogger("aurelia.happenings")
router = APIRouter(prefix="/pulsar", tags=["pulsar"])

STATE = "aurelia:pulsar:state:{}"                  # the last word heard for each device (source|id)
ON_KINDS = {"light", "switch", "fan", "input_boolean"}          # entities whose state is plainly on or off
OPEN_KINDS = {"door", "window", "cover"}
# (his words, the state they mean, the kinds they apply to, how I read it back)
VERBS = [
    (r"unlocks|is\s+unlocked|gets\s+unlocked|is\s+left\s+unlocked", "unlocked", {"lock"}, "unlocks"),
    (r"locks|is\s+locked|gets\s+locked", "locked", {"lock"}, "locks"),
    (r"turns?\s+on|is\s+turned\s+on|comes\s+on|goes\s+on|switches\s+on", "on", ON_KINDS, "turns on"),
    (r"turns?\s+off|is\s+turned\s+off|goes\s+off|switches\s+off", "off", ON_KINDS, "turns off"),
    (r"opens|is\s+opened|gets\s+opened|is\s+open", "open", OPEN_KINDS, "opens"),
    (r"closes|is\s+closed|gets\s+closed|shuts", "closed", OPEN_KINDS, "closes"),
    (r"detects\s+(?:water|a\s+leak)|gets\s+wet|is\s+wet|finds\s+water", "wet", {"leak_sensor"}, "detects water"),
    (r"detects\s+(?:motion|movement)|sees\s+(?:motion|movement|someone)", "motion", {"motion_sensor"}, "detects motion"),
    (r"gets\s+(?:back\s+)?home|arrives(?:\s+home)?|comes\s+home|is\s+home", "home", {"person"}, "gets home"),
    (r"leaves(?:\s+home)?|goes\s+out|is\s+away", "away", {"person"}, "leaves home"),
]
_VERB = "|".join(f"(?:{v[0]})" for v in VERBS)
_ME_HOME = r"(?:i\s+(?:get|come|arrive)\s+(?:back\s+)?home|i\s+arrive|i\s+get\s+back|i'?m\s+home|i\s+am\s+home)"
_ME_AWAY = r"(?:i\s+leave(?:\s+home|\s+the\s+house)?|i\s+go\s+out|i'?m\s+away)"
_WHEN = r"(?:when(?:ever)?|each\s+time|every\s+time|any\s+time)"
# When it may run, said right after the event: "during quiet hours", "between 5pm and 6pm", "after 9pm",
# "before 7am", "on weekdays", "on Mondays and Fridays" (several at once: all must hold).
_TIME = (r"(?:\d{1,2}(?!\d)(?::\d{2})?\s*(?:[ap]\.?\s?m\.?)?|noon|midnight)"
         r"(?!\s*(?:minutes?|mins?|hours?|hrs?|seconds?|secs?|days?|weeks?)\b)")      # "after 10 minutes" isn't a time
_DAYS_SAID = (r"(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)s?"
              r"(?:(?:\s*(?:,|and|&|or)\s*(?:and\s+)?|\s+)(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|"
              r"fri(?:day)?|sat(?:urday)?|sun(?:day)?)s?)*(?!\w)")
_ONE_COND = (rf"(?:(?:during|in)\s+(?:the\s+)?quiet\s+hours|at\s+night|overnight|"
             rf"between\s+{_TIME}\s+and\s+{_TIME}|from\s+{_TIME}\s+(?:to|until|till|til)\s+{_TIME}|"
             rf"after\s+{_TIME}|before\s+{_TIME}|"
             rf"(?:on\s+)?(?:weekdays|weeknights|weekends|the\s+weekend)|on\s+{_DAYS_SAID})")
_COND = rf"(?P<cond>(?:\s*,?\s+(?:and\s+|but\s+only\s+|only\s+)?{_ONE_COND})*)"
_QUIET = _COND                                                    # (its old name)
_HOOK = r"(?:the\s+|my\s+)?(?P<hook>[\w' -]+?)\s+(?:web)?hook\s+(?:is\s+called|gets\s+called|fires|is\s+triggered|goes\s+off|rings)"
_THING = rf"(?:{_ME_HOME}|{_ME_AWAY}|{_HOOK}|(?:the\s+|my\s+)?(?P<dev>[\w' -]+?)\s+(?P<verb>{_VERB}))"
FIRST = re.compile(rf"^{_WHEN}\s+(?P<ev>{_THING}){_QUIET}\s*,?\s+(?:then\s+)?(?P<what>.+)$", re.I)
_NOT_A_QUESTION = (r"(?!(?:what|when|where|who|why|how|is|are|was|were|do|does|did|can|could|will|would|should|which|"
                   r"tell|check|show)\b)")
LAST = re.compile(rf"^{_NOT_A_QUESTION}(?P<what>.+?)\s+{_WHEN}\s+(?P<ev>{_THING}){_QUIET}$", re.I)


def word(kind: str, state: dict) -> str | None:
    """What a reported state means, in the same words as VERBS. None: nothing to go on (unavailable)."""
    if "state" in state:                                          # Home Assistant
        s = str(state.get("state") or "").lower()
        if s in ("", "unavailable", "unknown", "opening", "closing"):
            return None
        if kind in ("door", "window"):
            return {"on": "open", "off": "closed"}.get(s, s)
        if kind == "leak_sensor":
            return {"on": "wet", "off": "dry"}.get(s, s)
        if kind == "motion_sensor":
            return {"on": "motion", "off": "clear"}.get(s, s)
        if kind == "person":
            return "home" if s == "home" else "away"
        return s
    if kind == "lock" and isinstance(state.get("locked"), bool):     # Orbit (SmartRent)
        return "locked" if state["locked"] else "unlocked"
    if kind in ("light", "switch") and isinstance(state.get("on"), bool):
        return "on" if state["on"] else "off"
    if kind == "leak_sensor" and isinstance(state.get("active"), bool):
        return "wet" if state["active"] else "dry"
    if kind == "motion_sensor" and isinstance(state.get("active"), bool):
        return "motion" if state["active"] else "clear"
    return None


def _internal(request: Request) -> None:
    """Only Orbit or Lagrange: from my stack's own network, never through the proxy or from your home lab."""
    from . import peers
    host = request.client.host if request.client else None
    if request.headers.get("x-aurelia-device") or request.headers.get("x-forwarded-host") or not peers.from_stack(host):
        raise HTTPException(403, "Only from inside the stack.")


async def _fire(app, where: str, *args: Any) -> int:
    """Start each armed automation waiting for this (after its heads-up), once per cooldown, and only when its
    conditions hold (quiet hours, a time window, days)."""
    from . import engine, pulsar
    cool = float(pulsar.rules(app)["event_cooldown_minutes"]) * 60
    fired = 0
    for j in await db.pool().fetch(f"SELECT id, rule, last_run_at FROM jobs WHERE status='armed' AND {where}", *args):
        rule = json.loads(j["rule"]) if isinstance(j["rule"], str) else (j["rule"] or {})
        if not allowed(app, rule):                     # quiet hours, a time window, days
            continue
        if j["last_run_at"] and (engine.now() - j["last_run_at"]).total_seconds() < cool:
            continue
        fired += await engine.fire(app, j["id"], f"{rule.get('on')} {rule.get('entity_name') or rule.get('hook_name') or ''}")
    return fired


@router.post("/state")
async def state_changed(request: Request):
    """A device's state, as Orbit or Lagrange heard it. An event only when it changed from the last I heard."""
    _internal(request)
    b = await request.json()
    source, eid, kind = str(b.get("source") or ""), str(b.get("id") or ""), str(b.get("kind") or "")
    if not source or not eid:
        raise HTTPException(400, "source and id")
    now_word = word(kind, b.get("state") or {})
    if now_word is None:
        return {"event": None}
    key = STATE.format(f"{source}|{eid}")
    before = await request.app.state.redis.set(key, now_word, get=True, ex=30 * 86400)
    before = before.decode() if isinstance(before, bytes) else before
    if before is None and b.get("was") is not None:
        before = word(kind, {"state": b["was"]})              # Home Assistant says what it was
    if before is None or before == now_word:
        return {"event": None}
    fired = await _fire(request.app, "rule->>'on'='state' AND rule->>'source'=$1 AND rule->>'entity'=$2 AND rule->>'to'=$3",
                        source, eid, now_word)
    return {"event": now_word, "started": fired}


@router.post("/hook")
async def hook_called(request: Request):
    """A webhook in was called (Lagrange checked its secret). What it sent is data: it starts what waits for it."""
    _internal(request)
    b = await request.json()
    name = str(b.get("hook") or "")
    if not name:
        raise HTTPException(400, "hook")
    fired = await _fire(request.app, "rule->>'on'='hook' AND rule->>'hook'=$1", name)
    return {"started": fired}


# ---- reading it from his words -------------------------------------------------------------------------

ORBIT_DEVICES = "aurelia:happenings:orbit"
ORBIT_KEEP = 45                                     # seconds: one automation sentence asks Orbit once, not per reading


async def entities(app) -> list[dict]:
    """Everything whose changes I hear about: Orbit's devices (kept ORBIT_KEEP s) and Home Assistant's entities."""
    from . import pulsar
    from .tasks import StartTask, launch, wait_for_result
    out: list[dict] = []
    raw = await app.state.redis.get(ORBIT_DEVICES)
    if raw:
        out = json.loads(raw)
    elif await registry.resolve(app.state.redis, "orbit.status") is not None:
        try:
            tid = await launch(app, StartTask(tool="orbit.status", args={}, user=pulsar.SYSTEM_USER, trigger=pulsar.NAME,
                                              display="brief"))
            res = await wait_for_result(tid, 30)
            for d in ((res or {}).get("result") or {}).get("devices") or []:
                if d.get("name"):
                    out.append({"source": "orbit", "id": str(d["name"]).lower(), "name": d["name"], "kind": d.get("kind")})
            if out:
                await app.state.redis.set(ORBIT_DEVICES, json.dumps(out), ex=ORBIT_KEEP)
        except Exception as exc:
            log.warning("orbit devices: %s", exc)
    fn = await registry.get_function(app.state.redis, "lagrange")
    if fn is not None:
        try:
            async with net.session(10) as c:
                r = await c.get(f"{fn.url}/ha/entities")
                if r.is_success:
                    out += [e for e in r.json().get("entities") or [] if isinstance(e, dict)]
        except httpx.HTTPError as exc:
            log.warning("home assistant entities: %s", exc)
    return out


def _norm(t: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (t or "").lower()) if w not in ("the", "my", "a", "an", "sensor")}


def _pick(words: str, ents: list[dict], kinds: set[str]) -> dict:
    asked = _norm(words)
    fits = [e for e in ents if e.get("kind") in kinds]
    hits = [e for e in fits if asked and (asked <= _norm(e["name"]) or asked <= _norm(str(e["id"]).replace(".", " ").replace("_", " ")))]
    exact = [e for e in hits if _norm(e["name"]) == asked]
    hits = exact or hits
    if len(hits) > 1:
        mine = [e for e in hits if e["source"] == "orbit"]           # the same lock in Orbit and Home Assistant
        if len(mine) == 1 and all(_norm(e["name"]) == _norm(mine[0]["name"]) for e in hits):
            hits = mine
    if len(hits) != 1:
        kind_words = {"lock": "lock", "door": "door", "window": "window", "cover": "door or blind", "light": "light",
                      "switch": "switch", "leak_sensor": "leak sensor", "motion_sensor": "motion sensor", "person": "person"}
        what = " or ".join(sorted({kind_words.get(k, k) for k in kinds}))[:60]
        names = ", ".join(sorted({e["name"] for e in (hits or fits)})[:8]) or "none I can hear"
        raise ValueError(f"Which one: {names}?" if hits else f"I don't know a {what} called “{words}”. Yours: {names}.")
    return hits[0]


def _me(ents: list[dict], owner_name: str) -> dict:
    people = [e for e in ents if e.get("kind") == "person"]
    if len(people) > 1:
        mine = [e for e in people if _norm(owner_name) and _norm(owner_name) <= _norm(e["name"])]
        people = mine if len(mine) == 1 else people
    if len(people) != 1:
        raise ValueError("Which person in Home Assistant is you: " + ", ".join(e["name"] for e in people) + "?" if people
                         else "I can't tell when you come and go yet: connect Home Assistant (with its phone app "
                              "tracking you), or use a webhook from a Shortcut.")
    return people[0]


async def parse(app, text: str) -> tuple[dict, str] | None:
    """His words -> (an event rule, what to do), or None when they don't name a home event. ValueError with
    a plain question when they do but I can't tell which device."""
    m = FIRST.match(text) or LAST.match(text)
    if not m:
        return None
    ev = m.group("ev")
    rule: dict[str, Any] = {}
    if m.group("hook"):
        name = re.sub(r"[^a-z0-9]+", "_", m.group("hook").lower()).strip("_")
        rows = await db.pool().fetch("SELECT name, label FROM connections WHERE kind='hook_in' AND status <> 'draft'")
        hit = [r for r in rows if r["name"] == name or r["label"].lower() == m.group("hook").lower().strip()]
        if len(hit) != 1:
            have = ", ".join(r["label"] for r in rows) or "none yet (connect one: “connect a webhook”)"
            raise ValueError(f"I don't have a webhook called “{m.group('hook').strip()}”. Yours: {have}.")
        rule = {"on": "hook", "hook": hit[0]["name"], "hook_name": hit[0]["label"]}
    else:
        ents = await entities(app)
        if re.fullmatch(_ME_HOME, ev, re.I) or re.fullmatch(_ME_AWAY, ev, re.I):
            owner = app.state.governance.charter.users.get("owner")
            e = _me(ents, owner.display_name if owner else "")
            home = bool(re.fullmatch(_ME_HOME, ev, re.I))
            rule = {"on": "state", "source": e["source"], "entity": e["id"], "entity_name": "you", "kind": "person",
                    "to": "home" if home else "away", "say": "get home" if home else "leave home"}
        else:
            verb = re.sub(r"\s+", " ", m.group("verb").lower())
            to, kinds, say = next((v[1], v[2], v[3]) for v in VERBS if re.fullmatch(v[0], verb, re.I))
            e = _pick(m.group("dev"), ents, kinds)
            rule = {"on": "state", "source": e["source"], "entity": e["id"], "entity_name": e["name"], "kind": e["kind"],
                    "to": to, "say": say}
    rule.update(conditions(m.group("cond")))
    return rule, m.group("what")


# ---- when it may run -------------------------------------------------------------------------------------

def _hhmm(said: str) -> str:
    """'5pm' -> '17:00'. A bare hour from 1 to 12 is asked about, never guessed (5 in the morning or evening?)."""
    from . import when
    s = when._clean(said)
    if s in ("noon", "midnight"):
        return "12:00" if s == "noon" else "00:00"
    c = when._clock(s.replace(" ", ""))
    if c is None:
        raise ValueError(f"I can't read “{said}” as a time.")
    h, mi, ap = c
    if ap is None:
        hm = f"{h}{f':{mi:02d}' if mi else ''}"
        if h == 12:
            raise ValueError(f"Do you mean {hm} noon or {hm} midnight? Say “noon” or “midnight”.")
        raise ValueError(f"Do you mean {hm} in the morning or the evening? Say “{hm}am” or “{hm}pm”.")
    if ap == "am":
        h = 0 if h == 12 else h
    elif ap == "pm":
        h = 12 if h == 12 else h + 12
    return f"{h:02d}:{mi:02d}"


def conditions(said: str | None) -> dict:
    """The when-it-may-run words after an event -> rule fields: only='quiet', window={'from','to'} (either may be
    None: "after 9pm" runs until midnight, "before 7am" from midnight), days=['mon', …]. ValueError to ask."""
    from . import when
    out: dict[str, Any] = {}
    t = " ".join((said or "").lower().replace(",", " ").split())
    if not t:
        return out
    for m in re.finditer(_ONE_COND, t, re.I):
        c = m.group(0)
        if re.match(r"(?:during|in)\s|at\s+night|overnight", c):
            out["only"] = "quiet"
            continue
        w = (re.fullmatch(rf"(?:between|from)\s+(?P<a>{_TIME})\s+(?:and|to|until|till|til)\s+(?P<b>{_TIME})", c) or
             re.fullmatch(rf"(?P<k>after|before)\s+(?P<a>{_TIME})", c))
        if w:
            if "window" in out:
                raise ValueError("One time window per automation, please: which one did you mean?")
            if "k" in w.groupdict() and w.group("k"):
                at = _hhmm(w.group("a"))
                if at == "00:00":                              # after midnight: all day; before midnight: never ends
                    raise ValueError(f"{w.group('k').capitalize()} midnight until when? Say “between midnight and 6am” "
                                     "(or “before 6am”).")
                if w.group("k") == "after" and at < "05:00":   # "after 1am" would run until midnight: nearly all day
                    raise ValueError(f"After {w.group('a').strip()} until when? Say “between {w.group('a').strip()} and 6am”.")
                out["window"] = {"from": at, "to": None} if w.group("k") == "after" else {"from": None, "to": at}
            else:
                a, b = _hhmm(w.group("a")), _hhmm(w.group("b"))
                if a == b:
                    raise ValueError("That window starts and ends at the same time.")
                out["window"] = {"from": a, "to": b}
            continue
        if "days" in out:
            raise ValueError("Say the days once (“on weekdays”, or “on Mondays and Fridays”).")
        if "weeknight" in c:                                   # which hours are "night"? never guessed
            raise ValueError("Which hours do you mean by weeknights? Say e.g. “after 9pm on weekdays”.")
        if "weekday" in c:
            out["days"] = when.ABBR[:5]
        elif "weekend" in c:
            out["days"] = when.ABBR[5:]
        else:
            d = re.sub(r"\btues", "tue", re.sub(r"\bthurs?", "thu", c))
            out["days"] = [a for a in when.ABBR if re.search(rf"\b{a}", d)]
    return out


def condition_in(words: str) -> str | None:
    """A time or day condition inside the part that says what to do ("turn on the light after 9pm"): it would be
    read as part of the command and never limit when it runs, so it's asked about instead of saved."""
    for m in re.finditer(rf"(?<!\w){_ONE_COND}", words or "", re.I):
        c = m.group(0).strip()
        if re.fullmatch(r"(?:weekdays|weeknights|weekends|the\s+weekend)", c, re.I):
            continue                                   # "remind me about the weekend plans": a word, not a condition
        return c
    return None


def allowed(app, rule: dict, at=None) -> bool:
    """Whether an event automation may run now: its quiet hours, time window and days (in his time zone). An
    overnight window ("between 10pm and 2am") belongs to the day it started on."""
    from datetime import datetime, time as dtime, timedelta, timezone
    from zoneinfo import ZoneInfo
    from . import engine, pulsar, when
    now = at or datetime.now(timezone.utc)
    if rule.get("only") == "quiet" and not pulsar.is_quiet(app, now):
        return False
    local = now.astimezone(ZoneInfo(engine.tz_name(app)))
    day, t = local.date(), local.time()
    if rule.get("only") == "quiet" and not rule.get("window"):     # a night's quiet hours belong to the day they began
        q = re.fullmatch(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", str(pulsar.quiet_spec(app) or "").replace(" ", ""))
        if q:
            qa, qb = dtime(int(q.group(1)), int(q.group(2))), dtime(int(q.group(3)), int(q.group(4)))
            if qa > qb and t < qb:
                day = day - timedelta(days=1)
    w = rule.get("window") or None
    if w:
        a = dtime.fromisoformat(w["from"]) if w.get("from") else dtime(0, 0)
        b = dtime.fromisoformat(w["to"]) if w.get("to") else None          # None: until midnight
        if b is None:
            inside = t >= a
        elif a < b:
            inside = a <= t < b
        else:                                                              # overnight
            inside = t >= a or t < b
            if inside and t < b:
                day = day - timedelta(days=1)
        if not inside:
            return False
    if rule.get("days"):
        if when.ABBR[day.weekday()] not in rule["days"]:
            return False
    return True


def say_conditions(rule: dict) -> str:
    """' during quiet hours', ' between 5:00 PM and 6:00 PM on weekdays' (empty when it always may)."""
    from . import when
    out = ""
    if rule.get("only") == "quiet":
        out += " during quiet hours"
    w = rule.get("window") or None
    if w:
        if w.get("from") and w.get("to"):
            out += f" between {when._clock12(w['from'])} and {when._clock12(w['to'])}"
        elif w.get("from"):
            out += f" after {when._clock12(w['from'])}"
        elif w.get("to"):
            out += f" before {when._clock12(w['to'])}"
    ds = rule.get("days") or []
    if ds:
        if set(ds) == set(when.ABBR[:5]):
            out += " on weekdays"
        elif set(ds) == set(when.ABBR[5:]):
            out += " on weekends"
        else:
            names = [when.DAYS[when.ABBR.index(d)].capitalize() + "s" for d in when.ABBR if d in ds]
            out += " on " + (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1])
    return out


def says(rule: dict) -> str | None:
    """How an event automation repeats, in his words ('when Front Door unlocks during quiet hours')."""
    on = rule.get("on")
    q = say_conditions(rule)
    if on == "state":
        who = rule.get("entity_name") or "it"
        return f"when {'you' if who == 'you' else who} {rule.get('say') or 'changes'}{q}"
    if on == "hook":
        return f"when the {rule.get('hook_name') or rule.get('hook')} webhook is called{q}"
    return None
