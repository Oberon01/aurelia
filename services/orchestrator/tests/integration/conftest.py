"""Shared setup for the end-to-end tests (Phases 3-4) against real Postgres (pgvector) and Redis.

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


DIGEST = {"quiet": False, "date": "Monday", "waiting": [], "waiting_count": 0, "automatic": [],
          "failures": [{"what": "Locking the door", "count": 1, "last_error": "jammed"}], "anomalies": [], "pattern": None}


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


@pytest.fixture(scope="session")
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
        "function: testfn\nrate_limit_per_minute: 1000\ntools:\n  status: {tier: reference}\n  switch: {tier: act}\n  look: {tier: reference}\n")
    (tmp / "gov" / "functions" / "parallax.yaml").write_text(
        "function: parallax\ntools:\n  digest: {tier: reference}\n  mailbox: {tier: reference}\n")
    (tmp / "gov" / "functions" / "mnemosyne.yaml").write_text(
        "function: mnemosyne\ntools:\n  recall: {tier: reference}\n  facts: {tier: reference}\n"
        "  remember: {tier: act}\n  forget: {tier: change}\n  history: {tier: reference}\n  roadmap: {tier: reference}\n"
        "rules:\n  quiet_minutes: 30\n  every_minutes: 600\n  exclude_tools: [testfn.switch]\n  max_facts: 6\n"
        "  max_core: 2\n  recall_min_score: 0.1\n")
    (tmp / "gov" / "functions" / "orbit.yaml").write_text("function: orbit\ntools:\n  tv_app: {tier: act}\n  tv_input: {tier: act}\n")
    (tmp / "gov" / "functions" / "quasar.yaml").write_text(
        "function: quasar\nrate_limit_per_minute: 1000\ntools:\n  find: {tier: reference}\n  play: {tier: act}\n")
    (tmp / "gov" / "functions" / "constellation.yaml").write_text(
        "function: constellation\nrate_limit_per_minute: 1000\ntools:\n  devices: {tier: reference}\n  open: {tier: act}\n  apps: {tier: reference}\n  home: {tier: act}\n"
        "  look: {tier: read}\n  click: {tier: act}\n  type: {tier: act}\n  press: {tier: act}\n  scroll: {tier: act}\n"
        "  back: {tier: act}\n  media: {tier: act}\n  submit: {tier: change}\n"
        "  health: {tier: reference}\n  list_processes: {tier: reference}\n  close_process: {tier: act}\n"
        "  kill_process: {tier: change}\n  sign_in: {tier: change}\n  logins: {tier: reference}\n  send: {tier: act}\n  clear: {tier: act}\n  screens: {tier: reference}\n  diagnose: {tier: read}\n  power: {tier: act}\n  restart: {tier: change}\n  wake: {tier: act}\n  sound: {tier: act}\n  watch: {tier: read}\n  install: {tier: act}\n  install_package: {tier: change}\n  uninstall: {tier: act}\n  remove_package: {tier: change}\n  clipboard: {tier: reference}\n  copy: {tier: act}\n  paste: {tier: act}\n  mode: {tier: act}\n"
        "rules:\n  screen:\n    confirm_before: [buy, place order, pay, send, delete]\n"
        "  screens:\n    - {computer: Den PC, tv: Den TV, input: HDMI 2}\n    - {computer: Attic PC, tv: Attic TV, input: ''}\n")
    (tmp / "aurelia.yaml").write_text(
        f"llm:\n  providers:\n    ollama: {{base_url: 'http://127.0.0.1:{fn_port}'}}\n"
        "memory:\n  maintenance_minutes: 60\n"
        "digest:\n  hour: 0\n  timezone: America/Chicago\n")

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
    settings.self_url = orch                          # Mnemosyne registers at the orchestrator's own address

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

    @fn.get("/digest")
    async def digest_preview():
        return DIGEST

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
    register_testfn(orch, f"http://127.0.0.1:{fn_port}")
    httpx.post(f"{orch}/registry/functions", json={
        "name": "parallax", "description": "test", "url": f"http://127.0.0.1:{fn_port}",
        "tools": [{"name": "digest", "description": "d", "input_schema": {"type": "object"}},
                  {"name": "mailbox", "description": "m", "input_schema": {"type": "object"}}]}).raise_for_status()

    yield {"orch": orch, "db": DB, "redis": REDIS, "fn": f"http://127.0.0.1:{fn_port}"}
    for s in servers:
        s.should_exit = True



def pytest_configure(config):
    config.addinivalue_line("markers", "node_env(**env): extra environment for the test's node agent")


def register_testfn(orch: str, fn: str) -> None:
    """The stand-in function's registration (it lapses in a long run; tests that use it renew it)."""
    import httpx
    httpx.post(f"{orch}/registry/functions", json={
        "name": "testfn", "description": "test", "url": fn,
        "tools": [{"name": "status", "description": "state", "input_schema": {"type": "object"},
                   "progress_unit": "devices", "freshness_seconds": 30},
                  {"name": "switch", "description": "act", "input_schema": {"type": "object"}},
                  {"name": "look", "description": "another look-up", "input_schema": {"type": "object"},
                   "freshness_seconds": 30}]}).raise_for_status()
