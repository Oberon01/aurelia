"""Memory (Phase 3): panel snapshots, retention tiers, restore, rollups, embeddings.

- Every task that returns something to read is saved as a panel snapshot, linked
  to its task_runs row (which is the permanent metadata).
- Asking for the same look-up again shows the last panel at once, labelled with
  its age (and "stale" past the tool's freshness window), while a fresh run
  replaces it. Only look-ups (reference/read tiers) restore; actions never show
  an old result as if it were new.
- A maintenance pass (hourly by default, one replica at a time) moves snapshots
  hot → warm → gone by age, keeps pinned ones, rolls up daily stats per tool,
  and embeds new snapshots for recall by meaning.
"""
import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import db
from . import net

log = logging.getLogger("aurelia.memory")
router = APIRouter(prefix="/memory", tags=["memory"])
LOCK_KEY = "aurelia:lock:memory-maintenance"
RESTORE_TIERS = {"reference", "read"}
TOP_ITEMS = 5


# ---- pure helpers (unit-tested) ------------------------------------------------------

def args_key(args: dict[str, Any] | None) -> str:
    """Normalised arguments, so 'Front Door ' and 'front door' are the same task."""
    def norm(v: Any) -> Any:
        if isinstance(v, str):
            return " ".join(v.lower().split())
        if isinstance(v, dict):
            return {k: norm(x) for k, x in sorted(v.items()) if x not in (None, "")}
        if isinstance(v, list):
            return [norm(x) for x in v]
        return v
    return json.dumps(norm(args or {}), sort_keys=True, separators=(",", ":"))


def summarize(data: Any, top: int = TOP_ITEMS) -> Any:
    """Warm-tier summary: same shape as the panel data, so it redraws as a lighter
    panel. Lists keep their first `top` items and record how many were cut in
    '_more'; long text is shortened."""
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        more: dict[str, int] = {}
        for k, v in data.items():
            if isinstance(v, list) and len(v) > top:
                more[k] = len(v) - top
                v = v[:top]
            out[k] = summarize(v, top)
        if more:
            out["_more"] = more
        return out
    if isinstance(data, list):
        return [summarize(x, top) for x in data]
    if isinstance(data, str) and len(data) > 300:
        return data[:297] + "…"
    return data


def source_text(tool: str, args: dict[str, Any], data: Any, limit: int = 2000) -> str:
    """Plain text of a result, for embedding and for word search."""
    lines = [tool.replace(".", " ").replace("_", " ")]
    if args:
        lines.append(" ".join(f"{k} {v}" for k, v in args.items()))

    def walk(v: Any, key: str = "") -> None:
        if sum(len(x) for x in lines) > limit:
            return
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, k)
        elif isinstance(v, list):
            for x in v:
                walk(x, key)
        elif v is not None and not (isinstance(v, str) and not v.strip()):
            lines.append(f"{key.replace('_', ' ')} {v}".strip())
    walk(data)
    return "\n".join(lines)[:limit]


def age_label(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def is_system_user(governance, user: str) -> bool:
    entry = governance.charter.users.get(user)
    return bool(entry and entry.role == "system")


def vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


# ---- saving and restoring ------------------------------------------------------------

async def save(task_id: str, tool: str, args: dict, user: str, data: dict, partial: bool) -> None:
    await db.pool().execute(
        "INSERT INTO panel_snapshots (task_id, tool, args, args_key, user_id, partial, panel, source_text) "
        "VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7::jsonb, $8) ON CONFLICT (task_id) DO NOTHING",
        uuid.UUID(task_id), tool, json.dumps(args), args_key(args), user, partial,
        json.dumps(data), source_text(tool, args, data),
    )


async def latest(tool: str, args: dict, user: str, freshness_seconds: int | None) -> dict[str, Any] | None:
    """The last saved panel for this exact look-up, ready for the display."""
    row = await db.pool().fetchrow(
        "SELECT task_id, captured_at, tier, pinned, partial, panel, summary FROM panel_snapshots "
        "WHERE user_id=$1 AND tool=$2 AND args_key=$3 ORDER BY captured_at DESC LIMIT 1",
        user, tool, args_key(args))
    if row is None:
        return None
    return snapshot_view(dict(row), freshness_seconds)


def snapshot_view(row: dict[str, Any], freshness_seconds: int | None) -> dict[str, Any]:
    age = (datetime.now(timezone.utc) - row["captured_at"]).total_seconds()
    body = row["panel"] if row["panel"] is not None else row["summary"]
    return {
        "task_id": str(row["task_id"]),
        "captured_at": row["captured_at"].isoformat(),
        "age_seconds": int(age),
        "age": age_label(age),
        "stale": freshness_seconds is not None and age > freshness_seconds,
        "tier": "pinned" if row["pinned"] else row["tier"],
        "partial": row["partial"],
        "data": json.loads(body) if isinstance(body, str) else (body or {}),
    }


# ---- embeddings ----------------------------------------------------------------------

class Embedder:
    """Local embeddings through Ollama. Optional: if Ollama isn't there, snapshots
    are still saved and searchable by words, and get embedded once it is."""

    def __init__(self, config_store):
        self.config = config_store

    def _settings(self) -> tuple[Any, str | None]:
        cfg = self.config.current
        e = cfg.memory.embeddings
        base = e.base_url or (cfg.llm.providers.get("ollama") or {}).get("base_url")
        return e, base

    @property
    def enabled(self) -> bool:
        e, base = self._settings()
        return e.enabled and bool(base)

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        e, base = self._settings()
        if not (e.enabled and base and texts):
            return None
        try:
            async with net.session(e.timeout_seconds) as client:
                r = await client.post(f"{base.rstrip('/')}/api/embed", json={"model": e.model, "input": texts})
                r.raise_for_status()
                vecs = r.json().get("embeddings") or []
        except (httpx.HTTPError, ValueError) as exc:
            log.info("embeddings unavailable (%s); will retry later", exc)
            return None
        if len(vecs) != len(texts) or any(len(v) != e.dimensions for v in vecs):
            log.warning("embedding model %s returned the wrong size; expected %d", e.model, e.dimensions)
            return None
        return vecs


# ---- maintenance ---------------------------------------------------------------------

async def maintain(config, embedder: Embedder | None = None, now: datetime | None = None) -> dict[str, int]:
    """Tiering, rollups and embedding backfill. Safe to run any time, any number of times."""
    cfg = config.current
    hot, warm = cfg.retention.hot_days, cfg.retention.warm_days
    pool = db.pool()
    now = now or datetime.now(timezone.utc)
    stats = {"to_warm": 0, "to_cold": 0, "days_rolled": 0, "embedded": 0}

    # hot → warm: keep a summary, drop the full payload (and the task row's copy of it).
    # Per tool: approved longer retention, or critical tools' longer default.
    rows = await pool.fetch(
        "SELECT s.task_id, s.panel FROM panel_snapshots s "
        "LEFT JOIN tool_retention r ON r.tool = s.tool LEFT JOIN tool_criticality c ON c.tool = s.tool "
        "WHERE s.tier='hot' AND NOT s.pinned AND s.captured_at < $1::timestamptz - make_interval(days => "
        "GREATEST($2, COALESCE(r.hot_days, 0), CASE WHEN c.level = 'critical' THEN $3 ELSE 0 END))",
        now, hot, cfg.retention.critical_hot_days)
    for r in rows:
        panel = json.loads(r["panel"]) if isinstance(r["panel"], str) else r["panel"]
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("UPDATE panel_snapshots SET tier='warm', summary=$2::jsonb, panel=NULL "
                               "WHERE task_id=$1", r["task_id"], json.dumps(summarize(panel or {})))
            await conn.execute("UPDATE task_runs SET result=NULL WHERE id=$1", r["task_id"])
    stats["to_warm"] = len(rows)

    # warm → cold: only the task_runs metadata remains.
    status = await pool.execute(
        "DELETE FROM panel_snapshots WHERE tier='warm' AND NOT pinned "
        "AND captured_at < $1::timestamptz - make_interval(days => $2)", now, warm)
    stats["to_cold"] = int(status.split()[-1])
    # Payloads of results that never had a snapshot (e.g. failed, or saved before Phase 3) age out too.
    await pool.execute(
        "UPDATE task_runs t SET result=NULL WHERE result IS NOT NULL "
        "AND started_at < $1::timestamptz - make_interval(days => $2) "
        "AND NOT EXISTS (SELECT 1 FROM panel_snapshots s WHERE s.task_id=t.id AND (s.pinned OR s.tier='hot'))",
        now, hot)

    # Daily rollups: every finished day not yet rolled up, and yesterday again (late finishers).
    status = await pool.execute("""
        INSERT INTO tool_stats_daily (day, tool, runs, completed, failed, cancelled, median_ms, p90_ms, worst_ms)
        SELECT started_at::date, tool, count(*),
               count(*) FILTER (WHERE status='completed'), count(*) FILTER (WHERE status='failed'),
               count(*) FILTER (WHERE status='cancelled'),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)::bigint,
               percentile_cont(0.9) WITHIN GROUP (ORDER BY duration_ms)::bigint,
               max(duration_ms)
        FROM task_runs
        WHERE started_at < date_trunc('day', $1::timestamptz)
          -- from the day before the last one rolled up (late finishers) on; everything the first time
          AND started_at >= COALESCE((SELECT max(day) FROM tool_stats_daily) - 1, '-infinity'::date)
          AND status <> 'running'
        GROUP BY 1, 2
        ON CONFLICT (day, tool) DO UPDATE SET runs=EXCLUDED.runs, completed=EXCLUDED.completed,
            failed=EXCLUDED.failed, cancelled=EXCLUDED.cancelled, median_ms=EXCLUDED.median_ms,
            p90_ms=EXCLUDED.p90_ms, worst_ms=EXCLUDED.worst_ms, rolled_at=now()""", now)
    stats["days_rolled"] = int(status.split()[-1])

    # Embeddings for new snapshots (and retries for ones that failed an hour ago).
    if embedder and embedder.enabled:
        todo = await pool.fetch(
            "SELECT task_id, source_text FROM panel_snapshots WHERE embedding IS NULL "
            "AND (embed_tried_at IS NULL OR embed_tried_at < now() - interval '1 hour') "
            "ORDER BY captured_at DESC LIMIT 64")
        if todo:
            vecs = await embedder.embed([r["source_text"] for r in todo])
            ids = [r["task_id"] for r in todo]
            if vecs is None:
                await pool.execute("UPDATE panel_snapshots SET embed_tried_at=now() WHERE task_id = ANY($1)", ids)
            else:
                await pool.executemany("UPDATE panel_snapshots SET embedding=$2::vector, embed_tried_at=now() "
                                       "WHERE task_id=$1", [(tid, vector_literal(vec)) for tid, vec in zip(ids, vecs)])
                stats["embedded"] = len(vecs)
    stats.update(await prune(cfg))
    return stats


BATCH = 5000                                          # rows per delete, so no one statement holds things up


async def _delete(sql: str, *args) -> int:
    """Delete in batches until nothing's left (sql selects ctids, LIMIT {BATCH})."""
    n = 0
    while True:
        status = await db.pool().execute(sql, *args)
        got = int(status.split()[-1])
        n += got
        if got < BATCH:
            return n


async def prune(cfg) -> dict[str, int]:
    """History that would otherwise grow forever: kept retention.history_days (a pinned panel keeps its task);
    my own background jobs' run log retention.system_runs_days. The daily rollups (tool_stats_daily) stay."""
    days, sys_days = int(cfg.retention.history_days), int(cfg.retention.system_runs_days)
    out = {}
    out["runs_pruned"] = await _delete(
        "DELETE FROM job_runs WHERE ctid IN (SELECT r.ctid FROM job_runs r JOIN jobs j ON j.id = r.job_id "
        "WHERE (j.created_by = 'system' AND r.started_at < now() - make_interval(days => $2)) "
        "OR r.started_at < now() - make_interval(days => $1) LIMIT %d)" % BATCH, days, sys_days)
    out["jobs_pruned"] = await _delete(
        "DELETE FROM jobs WHERE ctid IN (SELECT ctid FROM jobs WHERE status IN ('done','cancelled','missed','failed') "
        "AND updated_at < now() - make_interval(days => $1) LIMIT %d)" % BATCH, min(days, 90))
    out["tasks_pruned"] = await _delete(
        "DELETE FROM task_runs WHERE ctid IN (SELECT t.ctid FROM task_runs t WHERE t.started_at < now() - make_interval(days => $1) "
        "AND status <> 'running' AND NOT EXISTS (SELECT 1 FROM panel_snapshots s WHERE s.task_id = t.id AND s.pinned) "
        "LIMIT %d)" % BATCH, days)
    out["requests_pruned"] = await _delete(
        "DELETE FROM ask_requests WHERE ctid IN (SELECT ctid FROM ask_requests WHERE created_at < now() - make_interval(days => $1) "
        "LIMIT %d)" % BATCH, days)
    out["model_calls_pruned"] = await _delete(
        "DELETE FROM llm_calls WHERE ctid IN (SELECT ctid FROM llm_calls WHERE at < now() - make_interval(days => $1) "
        "LIMIT %d)" % BATCH, days)
    out["denials_pruned"] = await _delete(
        "DELETE FROM governance_denials WHERE ctid IN (SELECT ctid FROM governance_denials "
        "WHERE at < now() - make_interval(days => $1) LIMIT %d)" % BATCH, min(days, 90))
    return out


async def maintenance_job(app, spec: dict) -> dict | None:
    """Tiering, rollups and the embedding backfill: one of her own jobs on Admiral's engine (it runs once per
    round across orchestrators, survives restarts, and its runs are logged)."""
    stats = await maintain(app.state.config, app.state.embedder)
    if any(stats.values()):
        log.info("memory maintenance: %s", stats)
    return {"note": ", ".join(f"{k} {v}" for k, v in stats.items() if v) or None}


def _register() -> None:
    from . import engine
    engine.system("memory.maintenance",
                  every=lambda app: max(5, app.state.config.current.memory.maintenance_minutes))(maintenance_job)


_register()


# ---- API -----------------------------------------------------------------------------

class Pin(BaseModel):
    pinned: bool = True


@router.get("/snapshots")
async def list_snapshots(tool: str | None = None, user: str = "owner", limit: int = 20):
    rows = await db.pool().fetch(
        "SELECT task_id, tool, args, captured_at, tier, pinned, partial FROM panel_snapshots "
        "WHERE user_id=$1 AND ($2::text IS NULL OR tool=$2) ORDER BY captured_at DESC LIMIT $3",
        user, tool, min(max(limit, 1), 200))
    now = datetime.now(timezone.utc)
    return {"snapshots": [{
        "task_id": str(r["task_id"]), "tool": r["tool"], "args": json.loads(r["args"]),
        "captured_at": r["captured_at"].isoformat(), "age": age_label((now - r["captured_at"]).total_seconds()),
        "tier": "pinned" if r["pinned"] else r["tier"], "partial": r["partial"]} for r in rows]}


@router.get("/snapshots/{task_id}")
async def get_snapshot(task_id: str):
    row = await db.pool().fetchrow(
        "SELECT task_id, tool, captured_at, tier, pinned, partial, panel, summary FROM panel_snapshots "
        "WHERE task_id=$1", _uuid(task_id))
    if row is None:
        raise HTTPException(404, "no saved panel for that task (never saved, or aged out)")
    return {"tool": row["tool"], **snapshot_view(dict(row), None)}


@router.post("/snapshots/{task_id}/pin")
async def pin(task_id: str, body: Pin):
    """Pinned panels are kept in full regardless of age. Unpinning lets them age normally."""
    row = await db.pool().fetchrow(
        "UPDATE panel_snapshots SET pinned=$2 WHERE task_id=$1 RETURNING tier, panel IS NOT NULL AS full",
        _uuid(task_id), body.pinned)
    if row is None:
        raise HTTPException(404, "no saved panel for that task")
    return {"task_id": task_id, "pinned": body.pinned, "full": row["full"]}


@router.get("/recall")
async def recall(request: Request, q: str, user: str = "owner", limit: int = 5):
    """Past results by meaning (embeddings), or by words when embeddings aren't available."""
    limit = min(max(limit, 1), 50)
    pool = db.pool()
    embedder: Embedder = request.app.state.embedder
    vecs = await embedder.embed([q]) if embedder.enabled else None
    if vecs:
        rows = await pool.fetch(
            "SELECT task_id, tool, captured_at, tier, pinned, partial, panel, summary, "
            "1 - (embedding <=> $2::vector) AS score FROM panel_snapshots "
            "WHERE user_id=$1 AND embedding IS NOT NULL ORDER BY embedding <=> $2::vector LIMIT $3",
            user, vector_literal(vecs[0]), limit)
        how = "meaning"
    else:
        words = [w for w in re.findall(r"\w+", q.lower()) if len(w) > 2][:8] or [q.lower()]
        rows = await pool.fetch(
            "SELECT task_id, tool, captured_at, tier, pinned, partial, panel, summary, "
            "(SELECT count(*) FROM unnest($2::text[]) w WHERE lower(source_text) LIKE '%' || w || '%') AS score "
            "FROM panel_snapshots WHERE user_id=$1 "
            "AND lower(source_text) LIKE ANY (SELECT '%' || w || '%' FROM unnest($2::text[]) w) "
            "ORDER BY score DESC, captured_at DESC LIMIT $3", user, words, limit)
        how = "words"
    return {"matched_by": how, "results": [
        {"tool": r["tool"], "score": float(r["score"]), **snapshot_view(dict(r), None)} for r in rows]}


@router.get("/stats")
async def tool_stats(tool: str | None = None, days: int = 30):
    rows = await db.pool().fetch(
        "SELECT * FROM tool_stats_daily WHERE ($1::text IS NULL OR tool=$1) "
        "AND day >= current_date - $2::int ORDER BY day DESC, tool", tool, min(max(days, 1), 3650))
    return {"days": [{**dict(r), "day": r["day"].isoformat(), "rolled_at": r["rolled_at"].isoformat()} for r in rows]}


@router.post("/maintain")
async def run_maintenance(request: Request):
    """Run tiering, rollups and embedding now (it also runs on its own every hour)."""
    return await maintain(request.app.state.config, request.app.state.embedder)


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(404, "unknown task id")
