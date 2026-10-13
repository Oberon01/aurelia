"""What Probe reports about Aurelia's functions. Pure shaping, so it's testable
without the network: main.py gathers the inputs, these functions shape them.

Inputs, all from the orchestrator:
- registered: GET /registry/functions  (functions running now, with their tools)
- policies:   GET /governance/functions (every function with a policy, running or not)
- week, year: GET /tasks/summary?days=7 and /tasks/last (people's use, per tool; year only needs last_run)
- seen:       GET /registry/seen (every function that has ever run → last heartbeat)
- health:     each running function's own GET /health
"""
import difflib
from datetime import datetime, timezone
from typing import Any

STATUS_WORDS = {
    "online": "Running and healthy",
    "unhealthy": "Running, but its health check failed",
    "disabled": "Turned off in its policy",
    "no_policy": "Running, but has no policy, so nothing it offers can run",
    "offline": "Not running (it has run before)",
    "planned": "Not built yet (policy drafted)",
}


def ago(stamp: str | None, now: datetime | None = None) -> str | None:
    if not stamp:
        return None
    then = datetime.fromisoformat(stamp)
    s = int(((now or datetime.now(timezone.utc)) - then).total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def health_note(health: dict[str, Any] | None) -> str | None:
    """A function's /health minus the obvious: e.g. 'smartrent ready · tvs Living Room TV'."""
    if not health:
        return None
    parts = []
    for k, v in health.items():
        if k == "status":
            continue
        if isinstance(v, list):
            v = ", ".join(str(x.get("name", x)) if isinstance(x, dict) else str(x) for x in v) or "none"
        elif isinstance(v, dict):
            v = ", ".join(f"{a} {b}" for a, b in v.items())
        parts.append(f"{k.replace('_', ' ')} {v}")
    return " · ".join(parts) or None


def status_of(name: str, registered: dict, policies: dict, healthy: dict, seen: dict) -> str:
    if name in registered:
        if name not in policies:
            return "no_policy"
        if not policies[name]["enabled"]:
            return "disabled"
        return "online" if healthy.get(name) else "unhealthy"
    if name in policies and not policies[name]["enabled"]:
        return "disabled"
    return "offline" if seen.get(name) else "planned"


def _usage(tools: list[dict], function: str) -> dict[str, dict]:
    return {t["tool"].split(".", 1)[1]: t for t in tools if t["tool"].startswith(function + ".")}


def overview(registered: list[dict], policies: list[dict], week: list[dict], year: list[dict],
             health: dict[str, dict | None], seen: dict[str, str] | None = None,
             now: datetime | None = None) -> list[dict]:
    reg = {f["name"]: f for f in registered}
    pol = {p["function"]: p for p in policies}
    healthy = {n: bool(h and h.get("status") == "ok") for n, h in health.items()}
    seen = seen or {}
    rows = []
    for name in sorted(set(reg) | set(pol), key=lambda n: (n not in reg, n)):
        status = status_of(name, reg, pol, healthy, seen)
        used = _usage(week, name)
        tools = reg[name]["tools"] if name in reg else [{"name": t} for t in pol.get(name, {}).get("tools", {})]
        last = max((u["last_run"] for u in _usage(year, name).values()), default=None)
        rows.append({
            "name": name.capitalize(),
            "state": status,
            "state_text": STATUS_WORDS[status],
            "purpose": reg[name]["description"] if name in reg else None,
            "tools": len(tools),
            "runs_7d": sum(u["runs"] for u in used.values()),
            "failed_7d": sum(u["failed"] for u in used.values()),
            "last_used": ago(last, now),
            "health": health_note(health.get(name)),
            "last_seen": ago(seen.get(name), now) if status == "offline" else None,
        })
    return rows


def detail(name: str, registered: list[dict], policies: list[dict], policy: dict | None,
           week: list[dict], year: list[dict], health: dict | None, seen: dict[str, str] | None = None,
           now: datetime | None = None) -> dict:
    reg = {f["name"]: f for f in registered}
    pol = {p["function"]: p for p in policies}
    fn = reg.get(name)
    tiers = pol.get(name, {}).get("tools", {})
    healthy = {name: bool(health and health.get("status") == "ok")}
    seen = seen or {}
    status = status_of(name, reg, pol, healthy, seen)
    used, ever = _usage(week, name), _usage(year, name)
    specs = fn["tools"] if fn else [{"name": t} for t in tiers]
    tools = []
    for t in specs:
        u = used.get(t["name"], {})
        tools.append({
            "tool": t["name"],
            "what_it_does": t.get("description"),
            "tier": tiers.get(t["name"], "change (default)"),
            "runs_7d": u.get("runs", 0),
            "failed_7d": u.get("failed", 0),
            "typical": f"{u['median_ms']} ms" if u.get("median_ms") is not None else None,
            "last_used": ago((ever.get(t["name"]) or {}).get("last_run"), now),
        })
    rules = (policy or {}).get("policy", {}).get("rules") or {}
    out = {
        "function": name.capitalize(),
        "state": status,            # not "status": results already carry the task's own status
        "state_text": STATUS_WORDS[status],
        "purpose": fn["description"] if fn else None,
        "health": health_note(health),
        "rate_limit": f"{pol[name]['rate_limit_per_minute']} per minute" if name in pol else None,
        "last_seen": ago(seen.get(name), now) if status == "offline" else None,
        "tools": tools,
    }
    if rules:
        out["rules"] = rules
    return out


def find_name(said: str, names: list[str]) -> str:
    """'orbit', 'Orbit function', 'the orbt' → 'orbit'."""
    s = " ".join(said.lower().replace("function", " ").split()).removeprefix("the ").strip()
    if s in names:
        return s
    close = difflib.get_close_matches(s, names, n=1, cutoff=0.6)
    if close:
        return close[0]
    raise LookupError(f"I don't have a function called '{said}'. Functions: {', '.join(sorted(names))}.")
