"""Phase 7 step 1: pairing and keys end to end, against real Postgres and Redis.

The entry check is called the way Traefik's forwardAuth calls it (X-Forwarded-Uri,
X-Forwarded-Method); the rest of the flow is what a browser, a node and the server do.
"""
import asyncio
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import asyncpg
import httpx
import pytest

from .conftest import DB, REDIS

pytestmark = pytest.mark.skipif(not (DB and REDIS), reason="needs TEST_DATABASE_URL and TEST_REDIS_URL")
ROOT = Path(__file__).resolve().parents[2]


def sql(query, *args):
    async def go():
        conn = await asyncpg.connect(DB)
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()
    return asyncio.run(go())


def check(orch, path, key=None, cookie=None, method="GET", html=False):
    headers = {"X-Forwarded-Uri": path, "X-Forwarded-Method": method}
    if html:
        headers["Accept"] = "text/html,application/xhtml+xml"
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return httpx.get(f"{orch}/auth/check", headers=headers, cookies={"aurelia_key": cookie} if cookie else None,
                     follow_redirects=False)


def pair(orch, kind="browser", name="Chrome on Windows"):
    r = httpx.post(f"{orch}/pair/request", json={"kind": kind, "name": name, "info": {"os": "Windows 11"}})
    r.raise_for_status()
    return r.json()


def card(device_id):
    [row] = sql("SELECT id, status, proposal FROM mailbox_items WHERE fingerprint=$1", f"device:{device_id}")
    return row


def status(orch, p):
    return httpx.post(f"{orch}/pair/status", json={"device_id": p["device_id"], "claim": p["claim"]})


@pytest.fixture(autouse=True)
def clean():
    sql("DELETE FROM devices")
    sql("DELETE FROM mailbox_items WHERE kind='device'")
    yield


def test_nothing_gets_in_without_a_key(stack):
    orch = stack["orch"]
    r = check(orch, "/view", html=True)
    assert r.status_code == 401 and 'url=/pair?next=%2Fview"' in r.text                 # the browser resolves it
    assert check(orch, "/ask", method="POST").status_code == 401
    assert check(orch, "/mailbox/1/decide", key="aur_made_up", method="POST").status_code == 401
    for public in ("/pair", "/pair/request", "/pair/status", "/health"):
        assert check(orch, public).status_code == 200
    assert "Pair this device" in httpx.get(f"{orch}/pair").text


def test_a_browser_pairs_through_the_mailbox(stack):
    orch = stack["orch"]
    p = pair(orch)
    assert len(p["code"]) == 7 and status(orch, p).json() == {"status": "pending", "code": p["code"]}
    item = card(p["device_id"])
    assert item["status"] == "waiting" and p["code"] in item["proposal"]
    listed = httpx.get(f"{orch}/mailbox").json()["items"]
    assert any(i["kind"] == "device" and i["proposal"]["code"] == p["code"] for i in listed)

    out = httpx.post(f"{orch}/mailbox/{item['id']}/decide", json={"action": "modify", "changes": {"name": "Desk PC"}}).json()
    assert out["status"] == "modified" and "Desk PC" in out["done"]
    r = status(orch, p)
    assert r.json()["status"] == "approved" and "key" not in r.json()                  # a browser never sees its key
    key = r.cookies.get("aurelia_key")
    assert key and "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    assert status(orch, p).status_code == 404                                        # collected exactly once

    ok = check(orch, "/mailbox/1/decide", cookie=key, method="POST")
    [dev] = sql("SELECT id, name, key_hash FROM devices WHERE id=$1", p["device_id"])
    assert ok.status_code == 200 and ok.headers["x-aurelia-device"] == str(dev["id"]) and dev["name"] == "Desk PC"
    assert dev["key_hash"] != key                                                    # only a hash is kept


def test_a_node_key_reaches_only_the_node_api_and_revoking_is_immediate(stack):
    orch = stack["orch"]
    p = pair(orch, kind="node", name="Living room")
    httpx.post(f"{orch}/mailbox/{card(p['device_id'])['id']}/decide", json={"action": "approve"}).raise_for_status()
    got = status(orch, p).json()
    key = got["key"]
    assert got["status"] == "approved" and key.startswith("aur_")
    assert check(orch, "/nodes/hello", key=key, method="POST").status_code == 200
    assert check(orch, "/mailbox/1/decide", key=key, method="POST").status_code == 403    # can't approve anything
    assert check(orch, "/devices/1/revoke", key=key, method="POST").status_code == 403   # nor revoke anyone
    devices = httpx.get(f"{orch}/devices").json()["devices"]
    assert [d["name"] for d in devices if d["status"] == "approved"] == ["Living room"]
    httpx.post(f"{orch}/devices/{p['device_id']}/revoke").raise_for_status()
    assert check(orch, "/nodes/hello", key=key, method="POST").status_code == 401


def test_devices_are_approved_one_at_a_time_and_can_be_declined(stack):
    orch = stack["orch"]
    a, b = pair(orch, name="One"), pair(orch, name="Two")
    ids = [card(a["device_id"])["id"], card(b["device_id"])["id"]]
    res = httpx.post(f"{orch}/mailbox/decide-batch", json={"ids": ids, "action": "approve"}).json()["results"]
    assert all("one at a time" in r["error"] for r in res)
    httpx.post(f"{orch}/mailbox/{ids[0]}/decide", json={"action": "reject"}).raise_for_status()
    assert status(orch, a).json()["status"] == "declined"
    assert status(orch, b).json()["status"] == "pending"


def test_requests_expire_and_are_limited(stack):
    orch = stack["orch"]
    old = pair(orch, name="Old")
    sql("UPDATE devices SET requested_at = now() - interval '20 minutes' WHERE id=$1", old["device_id"])
    assert status(orch, old).json()["status"] == "expired"
    assert card(old["device_id"])["status"] == "expired"
    r = httpx.post(f"{orch}/mailbox/{card(old['device_id'])['id']}/decide", json={"action": "approve"})
    assert r.status_code == 409
    for i in range(5):
        pair(orch, name=f"D{i}")
    assert httpx.post(f"{orch}/pair/request", json={"name": "one too many"}).status_code == 429


def test_the_first_device_is_approved_on_the_server(stack):
    orch = stack["orch"]
    p = pair(orch, name="Phone")
    env = {**os.environ, "DATABASE_URL": DB, "REDIS_URL": REDIS}
    run = lambda *a: subprocess.run([sys.executable, "-m", "app.auth", *a], cwd=ROOT, env=env, capture_output=True, text=True)
    out = run("approve", p["code"].lower().replace("-", " "))
    assert out.returncode == 0 and "Approved" in out.stdout, out.stdout + out.stderr
    assert card(p["device_id"])["status"] == "approved"
    assert status(orch, p).cookies.get("aurelia_key")
    assert run("approve", "ZZZ-ZZZ").returncode == 1
    t = run("terminal", "atlas terminal")
    key = t.stdout.strip()
    assert t.returncode == 0 and check(orch, "/ask", key=key, method="POST").status_code == 200
    assert check(orch, "/mailbox", key=key).status_code == 403
    listing = run("list").stdout
    assert "Phone" in listing and "atlas terminal" in listing


def test_devices_panel_lists_them_through_aurelia(stack):
    orch = stack["orch"]
    p = pair(orch, name="Laptop")
    httpx.post(f"{orch}/mailbox/{card(p['device_id'])['id']}/decide", json={"action": "approve"}).raise_for_status()
    pair(orch, name="Waiting one")
    r = httpx.post(f"{orch}/tasks", json={"tool": "constellation.devices", "args": {}}).json()
    deadline = time.time() + 10
    while time.time() < deadline:
        res = httpx.get(f"{orch}/tasks/{r['task_id']}").json()
        if res.get("status") == "completed":
            break
        time.sleep(0.2)
    data = res["result"] if isinstance(res["result"], dict) else __import__("json").loads(res["result"])
    assert data["headline"] == "1 paired · 1 waiting"
    assert [(d["name"], d["status"]) for d in data["paired"]] == [("Laptop", "approved"), ("Waiting one", "pending")]
