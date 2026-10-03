"""Notifications end to end: a display turns them on, and his phone hears about what finished while
he wasn't looking, a test when he asks, and new urgent mailbox items. Real Postgres and Redis; the
push service itself is stood in for (what's sent is checked in tests/test_push.py)."""
import asyncio
import json
import os
import time
import uuid

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


def device(name, info=None):
    [row] = sql("INSERT INTO devices (kind, name, status, key_hash, approved_at, info) VALUES ('browser', $1, 'approved', $2, "
                "now(), $3::jsonb) RETURNING id", name, uuid.uuid4().hex, json.dumps(info or {}))
    return row["id"]


def keys():
    from cryptography.hazmat.primitives.asymmetric import ec
    from app import push
    k = ec.generate_private_key(ec.SECP256R1())
    return {"p256dh": push.b64u(push.public_raw(k)), "auth": push.b64u(os.urandom(16))}


def wait_for(pred, seconds=25):
    end = time.time() + seconds
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.25)
    return False


def test_his_phone_hears_what_finished_a_test_and_urgent_mailbox_items(stack, monkeypatch):
    from app import auth, push
    orch = stack["orch"]
    sql("DELETE FROM push_subscriptions")
    auth._by_id.clear()
    phone = device("iPhone", {"os": "iPhone"})
    h = {"X-Aurelia-Device": str(phone)}
    sent = []

    async def capture(sub, message, urgency="normal", transport=None, subject=""):
        sent.append((sub["device_id"], message, urgency))
        assert subject == "https://aurelia.example.ts.net"                    # a contact Apple accepts
        return True, ""
    monkeypatch.setattr(push, "send_one", capture)
    monkeypatch.setattr(push, "TASK_PUSH_SECONDS", 0)

    bad = httpx.post(f"{orch}/push/subscribe", headers=h, json={"endpoint": "https://evil.example/x", "keys": keys()})
    assert bad.status_code == 400
    ok = httpx.post(f"{orch}/push/subscribe", headers=h, json={"endpoint": "https://web.push.apple.com/QX" + uuid.uuid4().hex,
                                                                "keys": keys()})
    assert ok.json() == {"ok": True, "device": "iPhone"}
    assert len(httpx.get(f"{orch}/push/key").json()["key"]) > 80

    # Something he asked from the phone finished while it was in his pocket: the phone hears.
    httpx.post(f"{orch}/tasks", headers=h, json={"tool": "testfn.status", "args": {}}).raise_for_status()
    assert wait_for(lambda: any(m["title"] == "Done" for d, m, u in sent if d == phone)), sent

    # While he's looking at it, nothing buzzes.
    import redis as sync_redis
    from app.constellation import DISPLAY_SEEN, SCREEN_REPORT
    r = sync_redis.Redis.from_url(REDIS)
    r.set(DISPLAY_SEEN.format(phone), "1", ex=60)
    r.set(SCREEN_REPORT.format(phone), json.dumps({"panels": [], "visible": True, "at": time.time()}), ex=60)
    before = len(sent)
    httpx.post(f"{orch}/tasks", headers=h, json={"tool": "testfn.status", "args": {}}).raise_for_status()
    time.sleep(2)
    assert len(sent) == before

    # "Send me a test notification": to this device, even while it's open.
    tid = httpx.post(f"{orch}/tasks", headers=h, json={"tool": "constellation.notify", "args": {"action": "test"}}).json()["task_id"]
    assert wait_for(lambda: httpx.get(f"{orch}/tasks/{tid}").json()["status"] != "running", 10)
    t = httpx.get(f"{orch}/tasks/{tid}").json()
    assert t["status"] == "completed", t.get("error")
    assert any(m["title"] == "Aurelia" and m["tag"] == "test" for d, m, u in sent)
    r.delete(DISPLAY_SEEN.format(phone), SCREEN_REPORT.format(phone))

    # A new urgent mailbox item (a device asking to pair) reaches the phone.
    httpx.post(f"{orch}/pair/request", json={"kind": "node", "name": "Garage PC"}).raise_for_status()
    assert wait_for(lambda: any("Garage PC wants to pair" in m["title"] and u == "high" for d, m, u in sent), 25), sent
