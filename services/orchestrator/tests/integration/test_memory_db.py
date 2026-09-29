"""Phase 3 end to end against real Postgres (pgvector) and Redis.
Skipped unless TEST_DATABASE_URL and TEST_REDIS_URL are set (see conftest.py)."""
import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from .conftest import DB, REDIS

pytestmark = pytest.mark.skipif(not (DB and REDIS), reason="needs TEST_DATABASE_URL and TEST_REDIS_URL")


def run_task(stack, tool, user="owner", args=None):
    """Start a task, return its panel.open event data and the finished task."""
    import httpx
    import redis as redis_sync
    r = redis_sync.Redis.from_url(stack["redis"])
    sub = r.pubsub()
    sub.subscribe("aurelia:events")
    time.sleep(0.1)
    res = httpx.post(f"{stack['orch']}/tasks", json={"tool": tool, "args": args or {}, "user": user})
    res.raise_for_status()
    tid = res.json()["task_id"]
    opened, deadline = None, time.time() + 10
    while time.time() < deadline:
        m = sub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if not m:
            continue
        ev = json.loads(m["data"])
        if ev["task_id"] == tid and ev["type"] == "panel.open":
            opened = ev["data"]
        if ev["task_id"] == tid and ev["type"] == "panel.close":
            break
    sub.close()
    return tid, opened


def sql(stack, query, *args):
    import asyncpg

    async def go():
        conn = await asyncpg.connect(stack["db"])
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()
    return asyncio.run(go())


def test_repeated_lookup_restores_last_panel_with_age(stack):
    first, opened = run_task(stack, "testfn.status")
    assert "snapshot" not in opened
    assert opened["key"] == "{}" and opened["refreshes"] == [] and opened["tier"] == "reference"
    second, opened = run_task(stack, "testfn.status")
    snap = opened["snapshot"]
    assert snap["task_id"] == first and snap["age"] == "just now" and snap["stale"] is False
    assert snap["tier"] == "hot" and snap["data"]["devices"][0]["name"] == "Front Door"
    # The fresh run is saved too, and becomes the next restore.
    _, opened = run_task(stack, "testfn.status")
    assert opened["snapshot"]["task_id"] == second


def test_actions_and_system_users_never_restore_or_save(stack):
    run_task(stack, "testfn.switch")
    _, opened = run_task(stack, "testfn.switch")
    assert "snapshot" not in opened                       # act tier: an old result isn't shown as new
    tid, opened = run_task(stack, "testfn.status", user="chrysalis")
    assert "snapshot" not in opened
    assert not sql(stack, "SELECT 1 FROM panel_snapshots WHERE task_id=$1::uuid", tid)


def test_pin_tiers_rollups_embeddings_and_recall(stack):
    import httpx
    orch = stack["orch"]
    pinned, _ = run_task(stack, "testfn.status")
    assert httpx.post(f"{orch}/memory/snapshots/{pinned}/pin", json={"pinned": True}).json()["pinned"] is True

    # Embeddings, then recall by meaning.
    stats = httpx.post(f"{orch}/memory/maintain").json()
    assert stats["embedded"] >= 4
    hits = httpx.get(f"{orch}/memory/recall", params={"q": "is the front door locked"}).json()
    assert hits["matched_by"] == "meaning" and hits["results"][0]["tool"] == "testfn.status"

    # Eight days later: unpinned hot panels become summaries; the task row's payload is dropped.
    sql(stack, "UPDATE panel_snapshots SET captured_at = now() - interval '8 days'")
    sql(stack, "UPDATE task_runs SET started_at = now() - interval '8 days'")
    stats = httpx.post(f"{orch}/memory/maintain").json()
    assert stats["to_warm"] >= 3
    warm = sql(stack, "SELECT task_id FROM panel_snapshots WHERE tier='warm' AND tool='testfn.status' LIMIT 1")[0]
    view = httpx.get(f"{orch}/memory/snapshots/{warm['task_id']}").json()
    assert view["tier"] == "warm" and view["data"]["_more"] == {"devices": 2}
    assert sql(stack, "SELECT result FROM task_runs WHERE id=$1", warm["task_id"])[0]["result"] is None
    kept = httpx.get(f"{orch}/memory/snapshots/{pinned}").json()
    assert kept["tier"] == "pinned" and len(kept["data"]["devices"]) == 7          # pinned: full, any age

    # A warm restore is labelled as a summary.
    _, opened = run_task(stack, "testfn.status")
    assert opened["snapshot"]["tier"] in ("warm", "pinned") and opened["snapshot"]["stale"] is True

    # Ninety-odd days: only metadata remains, except the pinned panel.
    sql(stack, "UPDATE panel_snapshots SET captured_at = now() - interval '100 days' WHERE tier='warm'")
    stats = httpx.post(f"{orch}/memory/maintain").json()
    assert stats["to_cold"] >= 3
    assert sql(stack, "SELECT count(*) AS n FROM panel_snapshots WHERE tier='warm'")[0]["n"] == 0
    assert httpx.get(f"{orch}/memory/snapshots/{pinned}").status_code == 200
    assert sql(stack, "SELECT count(*) AS n FROM task_runs")[0]["n"] >= 8          # metadata kept

    # Daily rollups of finished days.
    days = httpx.get(f"{orch}/memory/stats", params={"tool": "testfn.status"}).json()["days"]
    assert days and days[0]["runs"] >= 4 and days[0]["completed"] == days[0]["runs"]
    assert days[0]["median_ms"] is not None


def test_recall_falls_back_to_words_without_embeddings(stack):
    import httpx
    sql(stack, "UPDATE panel_snapshots SET embedding = NULL")
    cfg = Path(os.environ["CONFIG_PATH"])
    cfg.write_text(cfg.read_text().replace("memory:\n", "memory:\n  embeddings: {enabled: false}\n"))
    time.sleep(2.5)                                                     # hot reload
    hits = httpx.get(f"{stack['orch']}/memory/recall", params={"q": "thermostat"}).json()
    assert hits["matched_by"] == "words" and hits["results"]
