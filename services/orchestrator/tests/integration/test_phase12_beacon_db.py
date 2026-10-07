"""Beacon's side of the orchestrator, end to end: times and days read with my own parser (/when/resolve, only from
my stack), critical notices also sent down the second path (a stand-in Beacon's /notify), and your calendar on the
day tape. Real Postgres and Redis; Beacon is a stand-in."""
import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
import pytest

from .conftest import DB, REDIS, free_port

pytestmark = pytest.mark.skipif(not (DB and REDIS), reason="needs TEST_DATABASE_URL and TEST_REDIS_URL")


def sql(query, *args):
    async def go():
        conn = await asyncpg.connect(DB)
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()
    return asyncio.run(go())


def _route(path, fn_):
    from app.main import app
    if not any(getattr(r, "path", "") == path for r in app.routes):
        app.add_api_route(path, fn_, methods=["POST"])


@pytest.fixture(scope="module")
def beacon(stack):
    """A stand-in Beacon: registers 'status' (so I can find it) and records what /notify was asked to send."""
    import uvicorn
    from fastapi import FastAPI
    port, seen = free_port(), {"notify": []}
    fn = FastAPI()

    @fn.post("/notify")
    async def notify(body: dict):
        seen["notify"].append(body)
        return {"sent": True}

    srv = uvicorn.Server(uvicorn.Config(fn, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    time.sleep(0.5)
    seen["register"] = lambda: httpx.post(f"{stack['orch']}/registry/functions", json={
        "name": "beacon", "description": "test", "url": f"http://127.0.0.1:{port}",
        "tools": [{"name": "status", "description": "status", "input_schema": {"type": "object", "properties": {}}}]
    }).raise_for_status()
    yield seen
    srv.should_exit = True


def test_times_and_days_are_read_with_my_own_parser_only_from_my_stack(stack):
    orch = stack["orch"]
    post = lambda **b: httpx.post(f"{orch}/when/resolve", json=b)
    r = post(text="dentist thursday at 3pm", need="split").json()
    at = datetime.fromisoformat(r["at"])
    assert r["what"] == "dentist" and at.hour == 15 and at.weekday() == 3 and at > datetime.now(timezone.utc)
    assert r["says"] and r["tz"] == "America/Chicago"
    r = post(text="tomorrow at 9am", need="time").json()
    assert datetime.fromisoformat(r["at"]).hour == 9
    assert post(text="this week", need="day").json()["days"] == 7
    assert post(text="next week", need="day").json()["days"] == 7
    d = post(text="thursday", need="day").json()
    assert datetime.fromisoformat(d["day"]).weekday() == 3 and d["days"] == 1
    for bad, need in (("dentist", "split"), ("sometime", "time"), ("whenever", "day")):
        r = post(text=bad, need=need)
        assert r.status_code == 422 and r.json()["detail"], (bad, r.text)          # asked, never guessed
    r = httpx.post(f"{orch}/when/resolve", json={"text": "today", "need": "day"}, headers={"x-forwarded-host": "x.ts.net"})
    assert r.status_code == 403


def test_a_critical_notice_also_goes_down_the_second_path(stack, beacon):
    from app import pulsar
    from app.main import app
    beacon["register"]()
    rules = app.state.governance.functions["pulsar"].rules

    async def raise_(body: dict):
        if body.get("clear"):
            return {"cleared": await pulsar.clear(app, body["kind"], body["key"])}
        n = await pulsar.notice(app, body["kind"], body["key"], body["title"], body.get("text", ""))
        return {"id": n["id"] if n else None}
    _route("/_test/beacon/notice", raise_)
    post = lambda **b: httpx.post(f"{stack['orch']}/_test/beacon/notice", json=b).json()
    old = dict(rules.get("severity") or {}), rules.get("ntfy_for")
    try:
        rules.setdefault("severity", {}).update(water_leak="critical", computer_down="high", budget_ahead="normal")
        rules["ntfy_for"] = ["critical"]
        n = len(beacon["notify"])
        crit = post(kind="water_leak", key="Kitchen-B", title="Water: Kitchen is wet", text="Check it now.")["id"]
        assert crit and len(beacon["notify"]) == n + 1
        sent = beacon["notify"][-1]
        assert sent == {"title": "Water: Kitchen is wet", "text": "Check it now.", "severity": "critical",
                        "tag": f"notice-{crit}"}
        post(kind="computer_down", key="Den-B", title="Den is down")             # high: not by default
        post(kind="budget_ahead", key="m-B", title="Budget ahead")
        assert len(beacon["notify"]) == n + 1
        rules["ntfy_for"] = ["critical", "high"]                                 # his rules can add high
        post(kind="computer_down", key="Desk-B", title="Desk is down")
        assert beacon["notify"][-1]["severity"] == "high"
    finally:
        rules["severity"], rules["ntfy_for"] = old
        for kind, key in (("water_leak", "Kitchen-B"), ("computer_down", "Den-B"), ("computer_down", "Desk-B"),
                          ("budget_ahead", "m-B")):
            post(kind=kind, key=key, clear=True, title="")


def test_your_calendar_is_on_the_day_tape(stack):
    orch = stack["orch"]
    soon = datetime.now(timezone.utc) + timedelta(hours=3)
    sql("DELETE FROM calendar_events WHERE uid LIKE 'tape-%'")
    sql("INSERT INTO calendar_events (source, uid, starts_at, ends_at, all_day, title) VALUES "
        "('mine', 'tape-1', $1, $2, false, 'Dentist'), ('google', 'tape-2', $1, $2, true, 'Holiday'), "
        "('google', 'tape-3', $3, $3, false, 'Next week')", soon, soon + timedelta(hours=1), soon + timedelta(days=8))
    try:
        items = httpx.get(f"{orch}/view/tape", timeout=10).json()["items"]
        ev = [i for i in items if i["kind"] == "event"]
        assert [(e["title"], e["source"]) for e in ev] == [("Dentist", "mine")]      # all-day and next week: not on it
        assert datetime.fromisoformat(ev[0]["at"]) == soon.replace(microsecond=soon.microsecond)
    finally:
        sql("DELETE FROM calendar_events WHERE uid LIKE 'tape-%'")
