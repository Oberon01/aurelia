"""The budget panel: this month's cloud tokens (his month, not UTC's), by model, its pace and reset, and the
cap itself counting the same month. Real Postgres and Redis."""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
import pytest

from .conftest import DB, REDIS

pytestmark = pytest.mark.skipif(not (DB and REDIS), reason="needs TEST_DATABASE_URL and TEST_REDIS_URL")


def sql(query, *args):
    async def go():
        conn = await asyncpg.connect(DB)
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()
    return asyncio.run(go())


def budget_panel(orch):
    tid = httpx.post(f"{orch}/tasks", json={"tool": "mnemosyne.budget", "args": {}}).json()["task_id"]
    deadline = time.time() + 10
    while time.time() < deadline:
        t = httpx.get(f"{orch}/tasks/{tid}").json()
        if t["status"] != "running":
            assert t["status"] == "completed", t["error"]
            return json.loads(t["result"]) if isinstance(t["result"], str) else t["result"]
        time.sleep(0.1)
    raise AssertionError("budget never finished")


def test_the_budget_panel_counts_his_month_by_model_and_says_when_it_resets(stack):
    from app import budget
    from app.main import app
    orch = stack["orch"]
    start, nxt = budget.month_bounds(datetime.now(timezone.utc), "America/Chicago")
    before = budget_panel(orch)["budget"]
    rows = [("claude", "claude-haiku-test", 300_000, start + timedelta(minutes=1)),
            ("claude", "claude-sonnet-test", 100_000, datetime.now(timezone.utc)),
            ("claude", "claude-haiku-test", 999_000, start - timedelta(minutes=1)),     # last month, by his clock
            ("ollama", "qwen-test", 50_000, datetime.now(timezone.utc))]                  # local: never counted
    for prov, model, n, at in rows:
        sql("INSERT INTO llm_calls (provider, model, input_tokens, output_tokens, at) VALUES ($1, $2, $3, 0, $4)",
            prov, model, n, at)
    try:
        out = budget_panel(orch)
        b = out["budget"]
        assert b["used"] - before["used"] == 400_000 and b["local_calls"] - before["local_calls"] == 1
        models = {m["model"]: m for m in b["models"]}
        assert models["claude-haiku-test"]["tokens_text"] == "300K" and "qwen-test" not in models
        assert b["cap"] == app.state.config.current.llm.monthly_token_cap and b["timezone"] == "America/Chicago"
        assert datetime.fromisoformat(b["resets_at"]) == nxt and b["resets"].endswith("12:00 AM")
        assert out["headline"].startswith(f"{round(b['used'] / b['cap'] * 100)}% of this month's budget used · resets in")
        assert "Anthropic's console" in b["note"]

        # the cap counts the same month: once it's spent, the panel says so and a plan won't start
        cfg = app.state.config.current.llm
        old = cfg.monthly_token_cap
        cfg.monthly_token_cap = 1000
        try:
            out = budget_panel(orch)
            assert out["budget"]["state"] == "reached" and out["headline"].startswith("Budget reached")
            tid = httpx.post(f"{orch}/tasks", json={"tool": "admiral.plan", "args": {"goal": "anything"}}).json()["task_id"]
            deadline = time.time() + 10
            while (t := httpx.get(f"{orch}/tasks/{tid}").json())["status"] == "running" and time.time() < deadline:
                time.sleep(0.1)
            assert t["status"] == "failed" and "month's model budget" in t["error"]
        finally:
            cfg.monthly_token_cap = old
    finally:
        sql("DELETE FROM llm_calls WHERE model LIKE '%-test'")
