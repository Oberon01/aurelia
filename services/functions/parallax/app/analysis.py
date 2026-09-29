"""What Parallax works out from Aurelia's task history. Plain statistics, no
language model: the evidence has to be reproducible and cheap.

Every function here takes rows of task history and returns proposals (for the
mailbox), insights, or the morning digest. Rows are dicts with: tool, args,
status, trigger, tier, started_at (aware datetime), duration_ms.

Only tasks Kentrell started himself (trigger "user") count as his habits;
display refreshes, digests, releases and system checks don't.
"""
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
LOOKUP_TIERS = {"reference", "read"}
NEVER_AUTOMATE = {"change", "never"}          # e.g. unlock: always asked in the moment, never scheduled


@dataclass
class Rules:
    timezone: str = "America/Chicago"
    window_days: int = 14          # recurrence: look back this far
    min_days: int = 5              # ...and need the habit on at least this many days
    min_confidence: float = 0.5    # ...on at least this share of the days it could have happened
    slot_minutes: int = 30         # "the same time" = within this window
    lead_minutes: int = 5          # look-ups are pre-run this much earlier
    crit_min_runs: int = 5         # criticality: judged only after this many runs in 30 days
    lookups_per_day: float = 3     # retention: proposed for look-ups used this often
    retention_days: int = 30
    drift_ratio: float = 2.0       # timing drift: this many times slower than usual
    drift_min_runs: int = 8

    @classmethod
    def from_policy(cls, rules: dict[str, Any] | None) -> "Rules":
        r = rules or {}
        rec, crit, ret, dr = (r.get(k) or {} for k in ("recurrence", "criticality", "retention", "drift"))
        return cls(timezone=r.get("timezone", cls.timezone),
                   window_days=rec.get("window_days", cls.window_days), min_days=rec.get("min_days", cls.min_days),
                   min_confidence=rec.get("min_confidence", cls.min_confidence),
                   slot_minutes=rec.get("slot_minutes", cls.slot_minutes), lead_minutes=rec.get("lead_minutes", cls.lead_minutes),
                   crit_min_runs=crit.get("min_runs", cls.crit_min_runs),
                   lookups_per_day=ret.get("lookups_per_day", cls.lookups_per_day),
                   retention_days=ret.get("hot_days", cls.retention_days),
                   drift_ratio=dr.get("ratio", cls.drift_ratio), drift_min_runs=dr.get("min_runs", cls.drift_min_runs))


# ---- words -------------------------------------------------------------------------------

def args_key(args: dict[str, Any] | None) -> str:
    """Same normalisation as the orchestrator's memory, so 'the same task' agrees."""
    def norm(v: Any) -> Any:
        if isinstance(v, str):
            return " ".join(v.lower().split())
        if isinstance(v, dict):
            return {k: norm(x) for k, x in sorted(v.items()) if x not in (None, "")}
        if isinstance(v, list):
            return [norm(x) for x in v]
        return v
    return json.dumps(norm(args or {}), sort_keys=True, separators=(",", ":"))


def action(tool: str, args: dict[str, Any]) -> str:
    """'check the home status', 'lock the front door', 'turn on the living room light'."""
    a = args or {}
    name = a.get("name")
    phrases = {
        "orbit.status": "check the home status",
        "orbit.lock": f"lock {name or 'the door'}", "orbit.unlock": f"unlock {name or 'the door'}",
        "orbit.light": f"turn {'on' if a.get('on', True) else 'off'} {name or 'the light'}"
                       if a.get("level") is None else f"set {name or 'the light'} to {a['level']}%",
        "orbit.thermostat": f"set the thermostat{' to ' + str(a['target']) + '°' if a.get('target') else ''}",
        "orbit.tv_power": f"turn the TV {'on' if a.get('on') else 'off'}",
        "orbit.tv_app": f"open {a.get('app', 'an app')} on the TV",
        "orbit.tv_input": f"switch the TV to {a.get('input', 'an input')}",
        "orbit.tv_status": "check the TV", "chrysalis.releases": "check recent releases",
        "chrysalis.status": "check for updates", "probe.functions": "check your functions",
        "parallax.mailbox": "open the mailbox",
    }
    if tool in phrases:
        return phrases[tool]
    fn, _, t = tool.partition(".")
    return f"run {fn.capitalize()}'s {t.replace('_', ' ')}"


def noun(tool: str) -> str:
    nouns = {"orbit.status": "home status checks", "orbit.lock": "locking the door", "orbit.unlock": "unlocking the door",
             "orbit.light": "lights", "orbit.thermostat": "thermostat changes", "orbit.tv_power": "turning the TV on and off",
             "chrysalis.releases": "release checks", "probe.functions": "function checks"}
    fn, _, t = tool.partition(".")
    return nouns.get(tool, f"{fn.capitalize()}'s {t.replace('_', ' ')}")


def clock(minutes: int) -> str:
    h, m = divmod(minutes % 1440, 60)
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


# ---- recurrence → automation proposals ------------------------------------------------------

def recurrences(rows: list[dict], now: datetime, rules: Rules) -> list[dict]:
    tz = ZoneInfo(rules.timezone)
    local_now = now.astimezone(tz)
    first_day = local_now.date() - timedelta(days=rules.window_days - 1)
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        if r["trigger"] != "user" or r.get("tier") in NEVER_AUTOMATE:
            continue
        when = r["started_at"].astimezone(tz)
        if when.date() >= first_day:
            groups[(r["tool"], args_key(r["args"]))].append({**r, "local": when})

    out = []
    for (tool, key), runs in groups.items():
        best = None
        for start in sorted({t["local"].hour * 60 + t["local"].minute for t in runs}):
            end = start + rules.slot_minutes
            hits = [t for t in runs if start <= t["local"].hour * 60 + t["local"].minute < end]
            days = {t["local"].date() for t in hits}
            # Days this habit could have shown up on: today only once its time has passed.
            possible = [first_day + timedelta(days=i) for i in range(rules.window_days)]
            possible = [d for d in possible if d < local_now.date()
                        or (d == local_now.date() and local_now.hour * 60 + local_now.minute >= end)]
            weekdays = {d for d in days if d.weekday() < 5}
            if len(weekdays) >= rules.min_days and len(days - weekdays) <= 1:
                kind, count = "weekdays", len(weekdays)
                eligible = len([d for d in possible if d.weekday() < 5])
            else:
                kind, count, eligible = "daily", len(days), len(possible)
            confidence = count / eligible if eligible else 0
            candidate = (count, confidence, kind, start, hits, eligible)
            if count >= rules.min_days and confidence >= rules.min_confidence and (best is None or candidate[:2] > best[:2]):
                best = candidate
        if best is None:
            continue
        count, confidence, kind, start, hits, eligible = best
        args = hits[-1]["args"] or {}
        lookup = hits[-1].get("tier") in LOOKUP_TIERS
        minutes = sorted(t["local"].hour * 60 + t["local"].minute for t in hits)
        typical = int(statistics.median(minutes))
        at = (typical - rules.lead_minutes) // 5 * 5 if lookup else round(typical / 5) * 5
        schedule_days = DAYS[:5] if kind == "weekdays" else DAYS
        span = "weekdays" if kind == "weekdays" else "days"
        what = action(tool, args)
        summary = (f"You did this on {count} of the last {eligible} {span}, "
                   f"usually between {clock(minutes[0])} and {clock(minutes[-1])}.")
        summary += (" Running it a few minutes early would have it ready when you ask." if lookup
                    else " Automating it means it happens even on days you forget.")
        effect = "Saved as an automation. It runs once Pulsar (Phase 10) is built"
        effect += "." if lookup else ", and it will still show a heads-up you can cancel every time."
        out.append({
            "kind": "automation", "fingerprint": f"automation:{tool}:{key}:{kind}",
            "title": f"{_cap(what)} {'every weekday' if kind == 'weekdays' else 'every day'} at {clock(at)}",
            "summary": summary,
            "proposal": {"tool": tool, "args": args,
                         "schedule": {"days": schedule_days, "time": f"{at // 60:02d}:{at % 60:02d}",
                                      "timezone": rules.timezone}},
            "evidence": [f"{t['local']:%a %b %-d} · {clock(t['local'].hour * 60 + t['local'].minute)}"
                         for t in sorted(hits, key=lambda t: t["local"], reverse=True)[:7]],
            "strength": count, "confidence": round(min(confidence, 1.0), 2), "effect": effect,
        })
    return sorted(out, key=lambda p: -p["confidence"])


# ---- criticality ----------------------------------------------------------------------------

TIER_RANK = {"reference": 0, "read": 1, "act": 2, "change": 3, "never": 4}


def criticality(rows: list[dict], now: datetime, rules: Rules, current: dict[str, str]) -> list[dict]:
    since = now - timedelta(days=30)
    mine = sorted((r for r in rows if r["trigger"] == "user" and r["started_at"] >= since), key=lambda r: r["started_at"])
    by_tool: dict[str, list[dict]] = defaultdict(list)
    for r in mine:
        by_tool[r["tool"]].append(r)
    out = []
    for tool, runs in by_tool.items():
        if len(runs) < rules.crit_min_runs:
            continue
        failures = [r for r in runs if r["status"] == "failed"]
        retried = sum(1 for f in failures if any(
            r is not f and f["started_at"] < r["started_at"] <= f["started_at"] + timedelta(minutes=5) for r in runs))
        per_week = len(runs) / (30 / 7)
        tier = max((r.get("tier") or "reference" for r in runs), key=lambda t: TIER_RANK.get(t, 0))
        score = (0.35 * min(1.0, per_week / 15) + 0.2                                   # frequency; you wait on it
                 + (0.3 * retried / len(failures) if failures else 0)                    # failures you chase
                 + {"act": 0.15, "change": 0.25}.get(tier, 0))                           # acts on the world
        score = round(score, 3)
        # "high" needs more than "you do it now and then and wait on it" (every task you start
        # scores that): frequency, chased failures, or acting on the world have to add up too.
        level = "critical" if score >= 0.65 else "high" if score >= 0.55 else "normal"
        if level == "normal" or TIER_RANK_LEVEL[level] <= TIER_RANK_LEVEL.get(current.get(tool, "normal"), 0):
            continue
        evidence = [f"Ran {len(runs)} times in the last 30 days (about {per_week:.0f} a week).",
                    "You started it yourself every time, so you're waiting on the result."]
        if failures:
            evidence.append(f"{len(failures)} failed; you tried again within 5 minutes {retried} "
                            f"time{'s' if retried != 1 else ''}.")
        if tier == "act":
            evidence.append("It acts on your home.")
        elif tier == "change":
            evidence.append("It's important enough that it always needs your confirmation.")
        effect = ("Its failures stand out on the display, and its panels are kept in full for 30 days instead of 7."
                  if level == "critical" else "Its failures stand out on the display.")
        out.append({"kind": "criticality", "fingerprint": f"criticality:{tool}:{level}",
                    "title": f"Treat {noun(tool)} as {level}",
                    "summary": f"{_cap(noun(tool))} look important to you: frequent, and you wait on them.",
                    "proposal": {"tool": tool, "level": level, "reason": " ".join(evidence)},
                    "evidence": evidence, "strength": len(runs), "confidence": round(min(score, 1.0), 2),
                    "effect": effect, "urgent": False})
    return sorted(out, key=lambda p: -p["confidence"])


TIER_RANK_LEVEL = {"normal": 0, "high": 1, "critical": 2}


# ---- retention ------------------------------------------------------------------------------

def retention(rows: list[dict], now: datetime, rules: Rules, kept: dict[str, int], critical: set[str]) -> list[dict]:
    since = now - timedelta(days=7)
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        if r["trigger"] == "user" and r["started_at"] >= since and r.get("tier") in LOOKUP_TIERS:
            counts[r["tool"]] += 1
    out = []
    for tool, n in counts.items():
        per_day = n / 7
        if per_day < rules.lookups_per_day or tool in critical or kept.get(tool, 0) >= rules.retention_days:
            continue
        out.append({"kind": "retention", "fingerprint": f"retention:{tool}",
                    "title": f"Keep full {noun(tool)} panels for {rules.retention_days} days",
                    "summary": f"You looked this up {n} times in the last week (about {per_day:.0f} a day). "
                               "Keeping full panels longer means the last one always restores in full.",
                    "proposal": {"tool": tool, "hot_days": rules.retention_days},
                    "evidence": [f"{n} look-ups in the last 7 days."], "strength": n,
                    "confidence": round(min(1.0, per_day / (rules.lookups_per_day * 2)), 2),
                    "effect": f"Full panels are kept {rules.retention_days} days instead of 7; after that, a summary as usual."})
    return out


# ---- timing drift (insight, not a proposal) ------------------------------------------------

def drift(rows: list[dict], now: datetime, rules: Rules) -> list[dict]:
    recent, before = defaultdict(list), defaultdict(list)
    for r in rows:
        if r["status"] != "completed" or r.get("duration_ms") is None:
            continue
        age = now - r["started_at"]
        if age <= timedelta(days=7):
            recent[r["tool"]].append(r["duration_ms"])
        elif age <= timedelta(days=35):
            before[r["tool"]].append(r["duration_ms"])
    out = []
    for tool in recent:
        a, b = recent[tool], before.get(tool, [])
        if len(a) < rules.drift_min_runs or len(b) < rules.drift_min_runs:
            continue
        now_ms, usual_ms = statistics.median(a), statistics.median(b)
        if usual_ms and now_ms / usual_ms >= rules.drift_ratio:
            out.append({"tool": tool, "ratio": round(now_ms / usual_ms, 1),
                        "text": f"{_cap(noun(tool))} took {now_ms / usual_ms:.1f}× longer this week "
                                f"(typically {_secs(now_ms)} vs {_secs(usual_ms)})."})
    return out


def _secs(ms: float) -> str:
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{ms:.0f} ms"


# ---- morning digest --------------------------------------------------------------------------

def digest(rows: list[dict], now: datetime, rules: Rules, waiting: list[dict], releases: list[dict],
           anomalies: list[dict]) -> dict:
    """Waiting items, what ran without you since yesterday, failures, anomalies, one
    notable pattern. Quiet (nothing to show) when none of the first four has anything."""
    tz = ZoneInfo(rules.timezone)
    since = now - timedelta(hours=24)
    failed: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["started_at"] >= since and r["status"] == "failed" and r["trigger"] in ("user", "automation"):
            failed[r["tool"]].append(r)
    failures = [{"what": _cap(noun(t)), "count": len(fs), "last_error": fs[-1].get("error")}
                for t, fs in sorted(failed.items(), key=lambda kv: -len(kv[1]))]
    automatic = [{"what": f"Release {r['sha'][:8]}: {(r.get('message') or '').splitlines()[0] if r.get('message') else ''}".strip(),
                  "outcome": r["status"]} for r in releases if r["status"] not in ("dry_run",)]
    automatic += [{"what": _cap(action(r["tool"], r["args"])), "outcome": r["status"]}
                  for r in rows if r["started_at"] >= since and r["trigger"] == "automation"]

    week = [r for r in rows if r["trigger"] == "user" and r["started_at"] >= now - timedelta(days=7)]
    pattern = None
    if week:
        top = max({r["tool"] for r in week}, key=lambda t: sum(1 for r in week if r["tool"] == t))
        n = sum(1 for r in week if r["tool"] == top)
        if n >= 3:
            pattern = f"Most used this week: {noun(top)}, {n} times."
    quiet = not (waiting or failures or automatic or anomalies)
    return {
        "date": f"{now.astimezone(tz):%A, %B %-d}",
        "quiet": quiet,
        "waiting": [{"id": w["id"], "title": w["title"], "kind": w["kind"]} for w in waiting[:5]],
        "waiting_count": len(waiting),
        "automatic": automatic[:8],
        "failures": failures[:6],
        "anomalies": [a["text"] for a in anomalies],
        "pattern": pattern,
    }
