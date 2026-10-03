"""My monthly model budget: how much of llm.monthly_token_cap this month has used, at what pace, and
when it starts over (midnight on the 1st, in his time zone, not the server's UTC).

This is my own cap on cloud model tokens (input + output, every Claude model, counted from llm_calls).
It is not his Anthropic account balance, which only Anthropic's console shows. Local models (Ollama)
don't count toward it.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import db

NEAR = 0.8                                   # from here on, the panel warns
OUTSIDE = "Your Anthropic credit balance is separate: Anthropic's console shows it. Local models don't count."


def tz_name(app=None) -> str:
    """His time zone (digest.timezone), which the month is counted in."""
    try:
        if app is None:
            from .main import app as _app
            app = _app
        return app.state.config.current.digest.timezone or "America/Chicago"
    except Exception:
        return "America/Chicago"


def month_bounds(now: datetime, tz: str) -> tuple[datetime, datetime]:
    """The start of this month and of the next one, at local midnight."""
    local = now.astimezone(ZoneInfo(tz))
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start, nxt.replace(tzinfo=ZoneInfo(tz))     # re-attach: the offset may differ (DST)


def _tokens(n: int) -> str:
    return f"{n / 1_000_000:.2f}M" if n >= 1_000_000 else f"{n / 1000:.0f}K" if n >= 1000 else str(n)


def _in(delta: timedelta) -> str:
    h = delta.total_seconds() / 3600
    if h < 1:
        return "in under an hour"
    if h < 36:
        return f"in {round(h)} hour{'s' if round(h) != 1 else ''}"
    d = round(h / 24)
    return f"in {d} days"


def _day(dt: datetime) -> str:
    return dt.strftime("%a, %b ") + str(dt.day)


async def budget(app, user: str, args: dict) -> dict:
    from .assistant import CLOUD_PROVIDERS
    cfg = app.state.config.current.llm
    tz = tz_name(app)
    now = datetime.now(ZoneInfo(tz))
    start, nxt = month_bounds(now, tz)
    cloud = list(CLOUD_PROVIDERS)
    rows = await db.pool().fetch(
        "SELECT model, count(*) AS calls, COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens FROM llm_calls "
        "WHERE provider = ANY($1::text[]) AND at >= $2 GROUP BY model ORDER BY tokens DESC", cloud, start)
    used = sum(int(r["tokens"]) for r in rows)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = await db.pool().fetchval(
        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM llm_calls WHERE provider = ANY($1::text[]) AND at >= $2",
        cloud, today_start) or 0
    week_from = max(start, today_start - timedelta(days=7))
    week = await db.pool().fetchval(
        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM llm_calls WHERE provider = ANY($1::text[]) "
        "AND at >= $2 AND at < $3", cloud, week_from, today_start) or 0
    local = await db.pool().fetchval(
        "SELECT count(*) FROM llm_calls WHERE NOT (provider = ANY($1::text[])) AND at >= $2", cloud, start) or 0

    cap = int(cfg.monthly_token_cap)
    pct = used / cap if cap else 1.0
    days_measured = max(1, (today_start - week_from).days)
    pace = (week / days_measured) if week else (int(today) / max(1 / 24, (now - today_start).total_seconds() / 86400) if today else 0)
    days_left = (nxt - now).total_seconds() / 86400
    projected = used + int(pace * days_left)
    hits = None
    if pace > 0 and used < cap and projected > cap:
        hits = now + timedelta(days=(cap - used) / pace)
    state = "reached" if used >= cap else "near" if pct >= NEAR else "ok"
    resets = f"{_day(nxt)}, 12:00 AM"
    if state == "reached":
        head = f"Budget reached · I start again {_in(nxt - now)} ({_day(nxt)})"
    else:
        head = f"{round(pct * 100)}% of this month's budget used · resets {_in(nxt - now)}"
    out = {
        "used": used, "cap": cap, "remaining": max(0, cap - used), "percent": round(min(pct, 1) * 100, 1),
        "used_text": _tokens(used), "cap_text": _tokens(cap), "remaining_text": _tokens(max(0, cap - used)),
        "state": state, "month": now.strftime("%B"),
        "resets_at": nxt.isoformat(), "resets": resets, "resets_in": _in(nxt - now), "timezone": tz,
        "today_text": _tokens(int(today)), "pace_text": f"{_tokens(int(pace))} a day" if pace else None,
        "projected_text": _tokens(projected) if pace else None,
        "projected_percent": round(projected / cap * 100) if cap and pace else None,
        "hits_cap": _day(hits) if hits else None,
        "models": [{"model": r["model"], "calls": int(r["calls"]), "tokens_text": _tokens(int(r["tokens"])),
                    "share": round(int(r["tokens"]) / used * 100) if used else 0} for r in rows],
        "local_calls": int(local),
        "note": ("This is my own monthly cap (llm.monthly_token_cap in config/aurelia.yaml), counted in your time "
                 "zone. " + OUTSIDE),
    }
    if state == "reached":
        out["note"] = ("I've used this month's cap, so I'm not calling Claude until it resets. Raising "
                       "llm.monthly_token_cap in config/aurelia.yaml takes effect right away. " + OUTSIDE)
    return {"headline": head, "budget": out}
