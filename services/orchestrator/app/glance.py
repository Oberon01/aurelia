"""What the display shows without being asked.

- GET /glance: the few things worth a glance right now, for the edges of the
  screen. Only what's off or in motion: a door left unlocked, a leak, a device
  offline or low on battery, something still running, an important action that
  failed. Nothing when all is normal.
- GET /suggest: what Kentrell usually asks next after a given tool, learned from
  his own requests (a follow-up he's made at least twice within a few minutes).

Both read what's already recorded (task runs, saved panels, requests); neither
starts anything.
"""
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request

from . import db

router = APIRouter(tags=["glance"])

HOME_HOURS = 24            # device states older than this aren't shown
RUNNING_HOURS = 2
FAILED_MINUTES = 60


def _age(ts: datetime) -> str:
    s = int((datetime.now(timezone.utc) - ts).total_seconds())
    return "just now" if s < 60 else f"{s // 60} min" if s < 3600 else f"{s // 3600} h"


def _load(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


def entities(data: dict) -> list[dict]:
    """Device states in a result, in the shapes Orbit returns (same rule as the display)."""
    out = []
    if not isinstance(data, dict):
        return out
    if data.get("kind") and data.get("name"):
        out.append(data)
    if isinstance(data.get("device"), str) and isinstance(data.get("locked"), bool):
        out.append({"name": data["device"], "kind": "lock", "locked": data["locked"], "online": True})
    for key in ("devices", "tvs"):
        for d in data.get(key) or []:
            if isinstance(d, dict) and d.get("name"):
                out.append(d)
    return out


def home_items(rows: list[dict]) -> list[dict[str, Any]]:
    """Newest state per device across recent Orbit results; only what's worth a look."""
    seen, items = set(), []
    for r in rows:                                   # newest first
        for d in entities(_load(r["panel"]) or {}):
            key = str(d["name"]).strip().lower()
            if key in seen:
                continue
            seen.add(key)
            when, name, kind = _age(r["captured_at"]), d["name"], d.get("kind")
            if d.get("online") is False:
                items.append({"level": "info", "text": f"{name} offline", "since": when})
            elif kind == "lock" and d.get("locked") is False:
                items.append({"level": "warn", "text": f"{name} unlocked", "since": when, "say": f"lock the {name.lower()}"})
            elif kind == "leak_sensor" and d.get("active"):
                items.append({"level": "bad", "text": f"Leak: {name}", "since": when})
            if isinstance(d.get("battery"), (int, float)) and d["battery"] < 20:
                items.append({"level": "info", "text": f"{name} battery {int(d['battery'])}%", "since": when})
    order = {"bad": 0, "warn": 1, "info": 2}
    return sorted(items, key=lambda i: order[i["level"]])


@router.get("/glance")
async def glance(request: Request, user: str = "owner"):
    pool = db.pool()
    system = [u for u, e in request.app.state.governance.charter.users.items() if e.role == "system"]
    items: list[dict[str, Any]] = []
    rows = [dict(r) for r in await pool.fetch(
        "SELECT panel, captured_at FROM panel_snapshots WHERE tool LIKE 'orbit.%' AND user_id=$1 AND panel IS NOT NULL "
        "AND captured_at > now() - make_interval(hours => $2) ORDER BY captured_at DESC LIMIT 50", user, HOME_HOURS)]
    items += home_items(rows)
    for r in await pool.fetch(
            "SELECT id, tool, progress_current, progress_total, progress_unit FROM task_runs WHERE status='running' "
            "AND trigger = 'user' AND started_at > now() - make_interval(hours => $1) AND user_id <> ALL($2::text[]) "
            "ORDER BY started_at DESC LIMIT 5", RUNNING_HOURS, system):
        pct = (round(100 * r["progress_current"] / r["progress_total"])
               if r["progress_total"] and r["progress_current"] is not None else None)
        items.append({"level": "live", "tool": r["tool"], "task_id": str(r["id"]), "percent": pct})
    for r in await pool.fetch(
            "SELECT t.tool, max(t.finished_at) AS at, count(*) AS n FROM task_runs t "
            "JOIN tool_criticality c ON c.tool = t.tool AND c.level IN ('high', 'critical') "
            "WHERE t.status='failed' AND t.finished_at > now() - make_interval(mins => $1) AND t.user_id <> ALL($2::text[]) "
            "GROUP BY t.tool", FAILED_MINUTES, system):
        items.append({"level": "bad", "tool": r["tool"], "failed": int(r["n"]), "since": _age(r["at"])})
    return {"items": items}


@router.get("/suggest")
async def suggest(tool: str, user: str = "owner", limit: int = 2):
    """Follow-ups he has made at least twice within 3 minutes of this tool running (last 30 days)."""
    rows = await db.pool().fetch(
        """WITH base AS (
             SELECT DISTINCT r.session_id, r.created_at FROM ask_requests r JOIN task_runs t ON t.id = ANY(r.task_ids)
             WHERE t.tool = $1 AND r.user_id = $2 AND r.created_at > now() - interval '30 days'),
           nxt AS (
             SELECT DISTINCT ON (b.session_id, b.created_at) lower(trim(n.text)) AS text
             FROM base b JOIN ask_requests n ON n.session_id = b.session_id
               AND n.created_at > b.created_at AND n.created_at <= b.created_at + interval '3 minutes'
             ORDER BY b.session_id, b.created_at, n.created_at)
           SELECT text, count(*) AS n FROM nxt WHERE text <> '(forgotten)' AND length(text) <= 60
           GROUP BY text HAVING count(*) >= 2 ORDER BY n DESC, text LIMIT $3""",
        tool, user, min(max(limit, 1), 4))
    return {"tool": tool, "usual": [{"say": r["text"], "times": r["n"]} for r in rows]}

