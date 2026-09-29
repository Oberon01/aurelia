"""Phase 4 end to end: the mailbox, what approvals change, and the digest.
Same real Postgres + Redis setup as the memory tests (skipped without them)."""
import httpx
import pytest

from .conftest import DB, REDIS
from .test_memory_db import run_task, sql

pytestmark = pytest.mark.skipif(not (DB and REDIS), reason="needs TEST_DATABASE_URL and TEST_REDIS_URL")


def propose(stack, **kw):
    body = {"kind": "automation", "fingerprint": "automation:testfn.status:{}:weekdays",
            "title": "Check the status every weekday at 7:25 AM", "summary": "You did this on 9 of 10 weekdays.",
            "proposal": {"tool": "testfn.status", "args": {},
                         "schedule": {"days": ["mon", "tue", "wed", "thu", "fri"], "time": "07:25"}},
            "evidence": ["Mon Sep 28 · 7:34 AM"], "strength": 9, "confidence": 0.9, "effect": "Saved."}
    body.update(kw)
    return httpx.post(f"{stack['orch']}/mailbox/proposals", json=body)


def count(stack):
    return httpx.get(f"{stack['orch']}/mailbox/count").json()["waiting"]


def decide(stack, item_id, **body):
    return httpx.post(f"{stack['orch']}/mailbox/{item_id}/decide", json=body)


def test_same_idea_updates_instead_of_duplicating(stack):
    first = propose(stack).json()
    again = propose(stack, confidence=0.95).json()
    assert first["result"] == "created" and again == {"id": first["id"], "result": "updated"}
    assert count(stack) == 1
    item = httpx.get(f"{stack['orch']}/mailbox/{first['id']}").json()
    assert item["confidence"] == 0.95 and item["status"] == "waiting"


def test_approve_with_changes_stores_the_automation(stack):
    item_id = propose(stack).json()["id"]
    bad = decide(stack, item_id, action="modify", changes={"schedule": {"time": "25:00", "days": ["mon"]}})
    assert bad.status_code == 400 and "07:30" in bad.json()["detail"]
    ok = decide(stack, item_id, action="modify", changes={"schedule": {"time": "07:20", "days": ["mon", "wed", "fri"]}})
    assert ok.json()["status"] == "modified" and "Pulsar" in ok.json()["done"]
    row = sql(stack, "SELECT tool, schedule, status FROM automations WHERE from_item=$1", item_id)[0]
    assert row["tool"] == "testfn.status" and '"07:20"' in row["schedule"] and row["status"] == "waiting_for_pulsar"
    assert decide(stack, item_id, action="approve").status_code == 409        # already decided
    assert propose(stack).json()["result"] == "already_approved"
    assert count(stack) == 0


def test_approved_criticality_marks_its_tasks(stack):
    item_id = propose(stack, kind="criticality", fingerprint="criticality:testfn.switch:critical",
                      title="Treat switching as critical", proposal={"tool": "testfn.switch", "level": "critical"}).json()["id"]
    _, opened = run_task(stack, "testfn.switch")
    assert "critical" not in opened                                            # not yet: only approval changes things
    assert decide(stack, item_id, action="approve").json()["status"] == "approved"
    _, opened = run_task(stack, "testfn.switch")
    assert opened["critical"] == "critical"


def test_declined_ideas_return_only_with_much_more_evidence(stack):
    fp = "retention:testfn.look"
    item_id = propose(stack, kind="retention", fingerprint=fp, title="Keep look panels 30 days",
                      proposal={"tool": "testfn.look", "hot_days": 30}, strength=10).json()["id"]
    decide(stack, item_id, action="reject", reason="not needed")
    assert propose(stack, kind="retention", fingerprint=fp, title="Keep", strength=12,
                   proposal={"tool": "testfn.look", "hot_days": 30}).json()["result"] == "declined_before"
    back = propose(stack, kind="retention", fingerprint=fp, title="Keep", strength=20,
                   proposal={"tool": "testfn.look", "hot_days": 30}, summary="Used a lot.").json()
    assert back["result"] == "created"
    item = httpx.get(f"{stack['orch']}/mailbox/{back['id']}").json()
    assert "you said: not needed" in item["summary"]
    decide(stack, back["id"], action="defer", days=7)
    assert count(stack) == 0
    sql(stack, "UPDATE mailbox_items SET defer_until = now() - interval '1 minute' WHERE id=$1", back["id"])
    assert count(stack) == 1                                                   # its date passed: back for review
    decide(stack, back["id"], action="approve")


def test_approved_retention_keeps_panels_longer(stack):
    tid, _ = run_task(stack, "testfn.look")
    sql(stack, "UPDATE panel_snapshots SET captured_at = now() - interval '8 days' WHERE task_id=$1::uuid", tid)
    httpx.post(f"{stack['orch']}/memory/maintain")
    assert sql(stack, "SELECT tier FROM panel_snapshots WHERE task_id=$1::uuid", tid)[0]["tier"] == "hot"   # 30 days now


def test_batch_approval(stack):
    ids = [propose(stack, kind="criticality", fingerprint=f"criticality:testfn.x{i}:high", title=f"x{i}",
                   proposal={"tool": f"testfn.x{i}", "level": "high"}).json()["id"] for i in range(3)]
    out = httpx.post(f"{stack['orch']}/mailbox/decide-batch", json={"ids": ids, "action": "approve"}).json()
    assert [r["status"] for r in out["results"]] == ["approved"] * 3
    assert len(sql(stack, "SELECT 1 FROM tool_criticality WHERE tool LIKE 'testfn.x%'")) == 3


def test_morning_digest_shows_once_a_day(stack):
    first = httpx.post(f"{stack['orch']}/digest/check").json()
    assert first["shown"] is True and first["task_id"]
    assert httpx.post(f"{stack['orch']}/digest/check").json() == {"shown": False, "why": "already shown today"}
    assert sql(stack, "SELECT content->>'quiet' AS q FROM digests")[0]["q"] == "false"
