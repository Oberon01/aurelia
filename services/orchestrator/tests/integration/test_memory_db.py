"""Phase 3 end to end against real Postgres (pgvector) and Redis.

Skipped unless TEST_DATABASE_URL and TEST_REDIS_URL are set (the Docker test
stage has neither). The database is wiped. Run locally with e.g.:

    TEST_DATABASE_URL=postgresql://postgres@/aurelia_test?host=/var/tmp&port=55432 \
    TEST_REDIS_URL=redis://localhost:56379/9 uv run python -m pytest -q tests/integration
"""
import asyncio
import hashlib
import json
import math
import os
import socket
import threading
import time
from pathlib import Path

import pytest

DB, REDIS = os.environ.get("TEST_DATABASE_URL"), os.environ.get("TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(not (DB and REDIS), reason="needs TEST_DATABASE_URL and TEST_REDIS_URL")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake_vector(text: str) -> list[float]:
    """Deterministic 768-d bag-of-words vector: texts sharing words point the same way."""
    v = [0.0] * 768
    for w in text.lower().split():
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 768] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    import asyncpg
    import httpx
    import redis as redis_sync
    import uvicorn
    from fastapi import FastAPI

    tmp = tmp_path_factory.mktemp("aurelia")
    fn_port, orch_port = free_port(), free_port()
    orch = f"http://127.0.0.1:{orch_port}"

    # config + governance
    (tmp / "gov" / "functions").mkdir(parents=True)
    (tmp / "gov" / "charter.yaml").write_text(
        "users:\n  owner: {role: owner}\n  chrysalis: {role: system}\n")
    (tmp / "gov" / "functions" / "testfn.yaml").write_text(
        "function: testfn\ntools:\n  status: {tier: reference}\n  switch: {tier: act}\n")
    (tmp / "aurelia.yaml").write_text(
        f"llm:\n  providers:\n    ollama: {{base_url: 'http://127.0.0.1:{fn_port}'}}\n"
        "memory:\n  maintenance_minutes: 60\n")

    # fresh database
    async def reset():
        conn = await asyncpg.connect(DB)
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.close()
    asyncio.run(reset())
    redis_sync.Redis.from_url(REDIS).flushdb()

    os.environ.update(DATABASE_URL=DB, REDIS_URL=REDIS, CONFIG_PATH=str(tmp / "aurelia.yaml"),
                      GOVERNANCE_PATH=str(tmp / "gov"))
    from app import migrate
    migrate.settings.database_url = DB
    asyncio.run(migrate.run())
    from app.main import app as orchestrator
    from app.settings import settings
    settings.database_url, settings.redis_url = DB, REDIS
    settings.config_path, settings.governance_path = str(tmp / "aurelia.yaml"), str(tmp / "gov")

    # a fake function (+ a fake Ollama embedding endpoint)
    fn = FastAPI()
    devices = [{"name": n, "kind": k, "online": True, **st} for n, k, st in [
        ("Front Door", "lock", {"locked": True}), ("Living Room Light", "light", {"on": True}),
        ("Bedroom Lamp", "light", {"on": False}), ("Thermostat", "thermostat", {"current_temp": 71}),
        ("Kitchen Leak", "leak_sensor", {"active": False}), ("Hall Motion", "motion_sensor", {"active": False}),
        ("Living Room TV", "tv", {"on": True, "app": "YouTube"})]]

    @fn.post("/tools/{tool}/invoke", status_code=202)
    async def invoke(tool: str, body: dict):
        async def work():
            async with httpx.AsyncClient() as c:
                await c.post(f"{orch}/tasks/{body['task_id']}/progress", json={"current": 1, "total": 1})
                data = {"devices": devices} if tool == "status" else {"name": "Lamp", "kind": "light", "on": True}
                await c.post(f"{orch}/tasks/{body['task_id']}/result", json={"status": "completed", "data": data})
        asyncio.create_task(work())
        return {}

    @fn.post("/api/embed")
    async def embed(body: dict):
        return {"embeddings": [fake_vector(t) for t in body["input"]]}

    servers = [uvicorn.Server(uvicorn.Config(a, host="127.0.0.1", port=p, log_level="warning"))
               for a, p in ((fn, fn_port), (orchestrator, orch_port))]
    for s in servers:
        threading.Thread(target=s.run, daemon=True).start()
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if httpx.get(f"{orch}/health").is_success:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    httpx.post(f"{orch}/registry/functions", json={
        "name": "testfn", "description": "test", "url": f"http://127.0.0.1:{fn_port}",
        "tools": [{"name": "status", "description": "state", "input_schema": {"type": "object"},
                   "progress_unit": "devices", "freshness_seconds": 30},
                  {"name": "switch", "description": "act", "input_schema": {"type": "object"}}]}).raise_for_status()

    yield {"orch": orch, "db": DB, "redis": REDIS}
    for s in servers:
        s.should_exit = True


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
    cfg.write_text(cfg.read_text() + "  embeddings: {enabled: false}\n")
    time.sleep(2.5)                                                     # hot reload
    hits = httpx.get(f"{stack['orch']}/memory/recall", params={"q": "thermostat"}).json()
    assert hits["matched_by"] == "words" and hits["results"]
