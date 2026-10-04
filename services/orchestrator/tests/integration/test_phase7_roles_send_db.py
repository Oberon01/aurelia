"""Node roles and sending: "send this to all presentation devices" reaches every presentation
display (a TV that isn't open gets it when it connects), a note reaches one display by name,
presentation displays don't get panels nobody sent them, and roles are set only on the devices
panel. Real Postgres, Redis and WebSockets."""
import asyncio
import json
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


def device(kind, name, info=None, role=None):
    [row] = sql("INSERT INTO devices (kind, name, status, key_hash, approved_at, info, role) "
                "VALUES ($1, $2, 'approved', $3, now(), $4::jsonb, $5) RETURNING id",
                kind, name, uuid.uuid4().hex, json.dumps(info or {}), role)
    return row["id"]


class Model:
    def __init__(self, steps):
        self.steps, self.seen = steps, []

    async def complete(self, route, system, messages, tools):
        from app.llm.base import Completion, ToolCall, Usage
        self.seen.append(tools)
        step = self.steps.pop(0) if self.steps else {"text": "Okay."}
        calls = [ToolCall(id=f"t{uuid.uuid4().hex[:8]}", name=c[0], args=c[1]) for c in step.get("calls", [])]
        return Completion(text=step.get("text", ""), tool_calls=calls, usage=Usage(10, 5), model="fake", provider="fake")


async def listen(url, dev, seconds, out, report=True):
    """A display: receives events and, like the real page, reports the panels it's showing.
    report=False is a display that's connected but frozen (a phone in a pocket)."""
    import websockets
    base = url.replace("ws", "http", 1).removesuffix("/ws")
    showing = []
    async with websockets.connect(url, additional_headers={"X-Aurelia-Device": str(dev)}) as ws, \
            httpx.AsyncClient() as c:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                e = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.3))
            except asyncio.TimeoutError:
                continue
            out.append(e)
            if report and e.get("type") == "panel.open" and e.get("data", {}).get("display") != "brief":
                if not any(e["task_id"] in p["tasks"] for p in showing):
                    showing.append({"n": len(showing) + 1, "title": e["tool"], "tool": e["tool"], "tasks": [e["task_id"]]})
                await c.post(f"{base}/view/screen", json={"panels": showing, "visible": True},
                             headers={"X-Aurelia-Device": str(dev)})
            elif report and e.get("type") == "panels.clear":                 # like the real page: close, then report
                nums = e.get("data", {}).get("panels")
                showing[:] = [p for p in showing if nums and p["n"] not in nums]
                await c.post(f"{base}/view/screen", json={"panels": showing, "visible": True},
                             headers={"X-Aurelia-Device": str(dev)})
            elif report and e.get("type") == "screen.check":                 # "report now", like the real page
                await c.post(f"{base}/view/screen", json={"panels": showing, "visible": True},
                             headers={"X-Aurelia-Device": str(dev)})


def opened(events):
    return [e for e in events if e.get("type") == "panel.open"]


def test_roles_and_sending(stack):
    from app import auth
    from app.main import app
    orch, ws_url = stack["orch"], stack["orch"].replace("http", "ws") + "/ws"
    sql("UPDATE devices SET status='revoked' WHERE status='approved'")     # only this test's displays
    auth._by_id.clear()
    desk = device("browser", "Desk", {"os": "Windows", "browser": "Chrome"})
    phone = device("browser", "Pixel", {"os": "Android", "browser": "Chrome"})
    tablet = device("browser", "Hall tablet", {"os": "Windows"})
    node = device("node", "Den TV PC", {"shell": "on", "screens": [{"w": 3840}], "hostname": "den"})
    tv = device("screen", "Den TV PC screen", {"node_id": str(node)})
    httpx.post(f"{orch}/registry/functions", json={
        "name": "testfn", "description": "test", "url": stack["fn"],
        "tools": [{"name": n, "description": n, "input_schema": {"type": "object"}} for n in ("status", "look")]
    }).raise_for_status()

    # A revoked device leaves the list at once (it doesn't linger for days).
    gone = device("browser", "Old laptop", {"os": "Windows"})
    httpx.post(f"{orch}/devices/{gone}/revoke").raise_for_status()
    assert "Old laptop" not in {d["name"] for d in httpx.get(f"{orch}/devices").json()["devices"]}

    # Roles: guessed, then chosen on the devices panel (and only there).
    listed = {d["name"]: d for d in httpx.get(f"{orch}/devices").json()["devices"]}
    assert (listed["Desk"]["role"], listed["Pixel"]["role"], listed["Den TV PC"]["role"]) == \
        ("workstation", "companion", "presentation")
    assert not listed["Den TV PC"]["role_chosen"] and "server" in listed["Den TV PC"]["roles"]
    assert httpx.post(f"{orch}/devices/{tablet}/role", json={"role": "server"}).status_code == 400
    assert httpx.post(f"{orch}/devices/{tablet}/role", json={"role": "presentation"}).json()["role"] == "presentation"
    me = httpx.get(f"{orch}/view/me", headers={"X-Aurelia-Device": str(tablet)}).json()
    assert me["role"] == "presentation"
    assert httpx.get(f"{orch}/view/me", headers={"X-Aurelia-Device": str(tv)}).json()["role"] == "presentation"
    amb = httpx.get(f"{orch}/ambient").json()
    assert amb["weather"] is None and isinstance(amb["quiet"], bool) and 0 < amb["dim"] <= 1

    # Something on the desk to send: panel 1.
    t1 = httpx.post(f"{orch}/tasks", json={"tool": "testfn.status", "args": {}},
                    headers={"X-Aurelia-Device": str(desk)}).json()["task_id"]
    deadline = time.time() + 10
    while httpx.get(f"{orch}/tasks/{t1}").json()["status"] == "running" and time.time() < deadline:
        time.sleep(0.2)
    screen = {"panels": [{"n": 1, "task_id": t1, "tool": "testfn.status", "title": "Home"}], "focus": None}

    got = {k: [] for k in ("desk", "phone", "tablet")}
    original = app.state.llm

    async def scenario():
        ls = [asyncio.create_task(listen(ws_url, d, 7, got[k])) for k, d in
              (("desk", desk), ("phone", phone), ("tablet", tablet))]
        await asyncio.sleep(1.0)
        async with httpx.AsyncClient(timeout=30) as c:
            app.state.llm = model = Model([{"calls": [("constellation__send", {"to": "all presentation devices"})]},
                                           {"text": "Sent."}])
            sent = (await c.post(f"{orch}/ask", headers={"X-Aurelia-Device": str(desk)}, json={
                "text": "send this to all presentation devices", "session_id": uuid.uuid4().hex,
                "screen": screen})).json()
            send_schema = next(t for t in model.seen[0] if t.name == "constellation__send").input_schema
            app.state.llm = Model([{"calls": [("constellation__send", {"to": "pixel", "text": "Grab milk",
                                                                        "url": "example.com/list"})]}, {"text": "Sent."}])
            note = (await c.post(f"{orch}/ask", headers={"X-Aurelia-Device": str(desk)}, json={
                "text": "send 'grab milk' to my pixel", "session_id": uuid.uuid4().hex})).json()
            app.state.llm = Model([{"calls": [("constellation__send", {"to": "the desk"})]}, {"text": "?"}])
            self_send = (await c.post(f"{orch}/ask", headers={"X-Aurelia-Device": str(desk)}, json={
                "text": "send this to the desk", "session_id": uuid.uuid4().hex, "screen": screen})).json()
            scheduled = (await c.post(f"{orch}/tasks", json={"tool": "testfn.look", "args": {"s": 1},
                                                              "trigger": "pulsar"})).json()["task_id"]
        await asyncio.gather(*ls)
        tv_got = []
        await listen(ws_url, tv, 2, tv_got)                    # the TV opens her afterwards
        return sent, send_schema, note, self_send, scheduled, tv_got

    try:
        sent, send_schema, note, self_send, scheduled, tv_got = asyncio.run(scenario())
    finally:
        app.state.llm = original

    assert "x-panel" not in json.dumps(send_schema)                        # the model gets plain JSON Schema
    [send_task] = sent["task_ids"]
    result = httpx.get(f"{orch}/tasks/{send_task}").json()
    assert result["status"] == "completed", result
    out = json.loads(result["result"]) if isinstance(result["result"], str) else result["result"]
    assert out["sent"]["confirmed"] == ["Hall tablet"] and out["sent"]["later"] == ["Den TV PC"]
    assert "Den TV PC" in out["note"] and "when it next opens" in out["note"]

    [shown] = [e for e in opened(got["tablet"]) if e["tool"] == "testfn.status"]
    assert shown["data"]["present"] is True and shown["data"]["sent_from"] == "Desk" and shown["device"] == tablet
    res = next(e for e in got["tablet"] if e["type"] == "panel.result" and e["task_id"] == shown["task_id"])
    assert res["data"]["devices"]                                          # the same panel, its data
    assert not [e for e in opened(got["phone"]) if e["tool"] == "testfn.status" and e.get("data", {}).get("sent_from")]
    assert not [e for e in got["desk"] if e.get("data", {}).get("sent_from")]   # the sender already has it

    [tv_shown] = [e for e in opened(tv_got) if e["tool"] == "testfn.status"]   # queued for the TV, delivered on connect
    assert tv_shown["data"]["present"] is True
    assert not sql("SELECT 1 FROM task_runs WHERE trigger='send' AND device=$1 AND status <> 'completed'", tv)

    [n] = [e for e in got["phone"] if e["type"] == "panel.result" and e["tool"] == "constellation.send"
           and "note" in e["data"] and isinstance(e["data"]["note"], dict)]
    assert n["data"]["note"] == {"text": "Grab milk", "url": "https://example.com/list"}
    assert n["data"]["headline"] == "From Desk"
    assert not [e for e in got["tablet"] if e.get("tool") == "constellation.send" and e["type"] == "panel.result"
                and isinstance(e["data"].get("note"), dict)]

    st = httpx.get(f"{orch}/tasks/{self_send['task_ids'][0]}").json()
    assert st["status"] == "failed" and "already here" in st["error"]

    # Nobody sent the presentation displays the scheduled panel.
    assert scheduled in {e["task_id"] for e in opened(got["phone"])}
    assert scheduled not in {e["task_id"] for e in opened(got["tablet"])}
    assert scheduled not in {e["task_id"] for e in opened(tv_got)}


def test_a_look_up_shown_on_another_display(stack):
    """'Send the device list to my iPhone': it runs once and opens only there, not here first."""
    from app import auth
    from app.main import app
    orch, ws_url = stack["orch"], stack["orch"].replace("http", "ws") + "/ws"
    sql("UPDATE devices SET status='revoked' WHERE status='approved'")
    auth._by_id.clear()
    desk = device("browser", "Desk", {"os": "Windows"})
    phone = device("browser", "iPhone", {"os": "iPhone"})
    httpx.post(f"{orch}/registry/functions", json={
        "name": "testfn", "description": "test", "url": stack["fn"],
        "tools": [{"name": n, "description": n, "input_schema": {"type": "object"}} for n in ("status", "look", "switch")]
    }).raise_for_status()
    got = {"desk": [], "phone": []}
    original = app.state.llm

    async def scenario():
        ls = [asyncio.create_task(listen(ws_url, d, 6, got[k])) for k, d in (("desk", desk), ("phone", phone))]
        await asyncio.sleep(1.0)
        async with httpx.AsyncClient(timeout=30) as c:
            app.state.llm = model = Model([{"calls": [("constellation__devices", {"_on": "my iphone"})]},
                                           {"text": "It's on your iPhone."}])
            shown = (await c.post(f"{orch}/ask", headers={"X-Aurelia-Device": str(desk)}, json={
                "text": "send the device list to my iphone", "session_id": uuid.uuid4().hex})).json()
            schema = next(t for t in model.seen[0] if t.name == "constellation__devices").input_schema
            app.state.llm = Model([{"calls": [("testfn__switch", {"_on": "my iphone"})]}, {"text": "No."}])
            acted = (await c.post(f"{orch}/ask", headers={"X-Aurelia-Device": str(desk)}, json={
                "text": "turn on the lamp on my iphone", "session_id": uuid.uuid4().hex})).json()
        await asyncio.gather(*ls)
        return shown, schema, acted

    try:
        shown, schema, acted = asyncio.run(scenario())
    finally:
        app.state.llm = original
    assert "_on" in schema["properties"]
    assert "reply_panel" not in shown and shown["reply"] == "It's on your iPhone."
    [run] = shown["task_ids"]
    assert sql("SELECT device FROM task_runs WHERE id=$1", uuid.UUID(run))[0]["device"] == 0
    on_phone = [e for e in opened(got["phone"]) if e["tool"] == "constellation.devices"]
    assert len(on_phone) == 1 and on_phone[0]["data"]["sent_from"] == "Desk"
    res = next(e for e in got["phone"] if e["type"] == "panel.result" and e["task_id"] == on_phone[0]["task_id"])
    assert any(d["name"] == "iPhone" for d in res["data"]["paired"])
    assert not [e for e in got["desk"] if e.get("tool") == "constellation.devices"]      # never opened here
    assert not acted["task_ids"]                                                     # an action isn't sent anywhere


def test_a_send_is_confirmed_or_kept_and_screens_says_what_is_where(stack):
    """Sending checks the panel arrived (from the display's own report). A display that's connected but
    frozen doesn't confirm: it's sent once more, then kept, and picked up when it's back on screen.
    'What's on my screens' lists each display's panels."""
    from app import auth
    from app.main import app
    orch, ws_url = stack["orch"], stack["orch"].replace("http", "ws") + "/ws"
    sql("UPDATE devices SET status='revoked' WHERE status='approved'")
    auth._by_id.clear()
    desk = device("browser", "Desk", {"os": "Windows"})
    phone = device("browser", "iPhone", {"os": "iPhone"})
    tab = device("browser", "Kitchen tablet", {"os": "Windows"}, role="presentation")
    httpx.post(f"{orch}/registry/functions", json={
        "name": "testfn", "description": "test", "url": stack["fn"],
        "tools": [{"name": n, "description": n, "input_schema": {"type": "object"}} for n in ("status", "look")]
    }).raise_for_status()
    got = {"phone": [], "tab": [], "desk": []}
    original = app.state.llm

    async def scenario():
        ls = [asyncio.create_task(listen(ws_url, phone, 26, got["phone"], report=False)),     # frozen in a pocket
              asyncio.create_task(listen(ws_url, tab, 26, got["tab"])),
              asyncio.create_task(listen(ws_url, desk, 26, got["desk"], report=False))]
        await asyncio.sleep(1.0)
        async with httpx.AsyncClient(timeout=60) as c:
            app.state.llm = Model([{"calls": [("testfn__status", {"_on": "everywhere"})]}, {"text": "Done."}])
            sent = (await c.post(f"{orch}/ask", headers={"X-Aurelia-Device": str(desk)}, json={
                "text": "show the house everywhere", "session_id": uuid.uuid4().hex})).json()
            tid = (await c.post(f"{orch}/tasks", json={"tool": "constellation.screens", "args": {}},
                                headers={"X-Aurelia-Device": str(desk)})).json()["task_id"]
            for _ in range(50):
                row = (await c.get(f"{orch}/tasks/{tid}")).json()
                if row["status"] != "running":
                    break
                await asyncio.sleep(0.2)
            # The phone comes back on screen: it picks up what was kept for it.
            kept = (await c.post(f"{orch}/view/queued", headers={"X-Aurelia-Device": str(phone)})).json()
            again = (await c.post(f"{orch}/view/queued", headers={"X-Aurelia-Device": str(phone)})).json()
            await c.post(f"{orch}/view/screen", headers={"X-Aurelia-Device": str(phone)}, json={   # it shows it now
                "visible": True, "panels": [{"n": 1, "title": "x", "tool": "testfn.status", "tasks": [kept["events"][0]["task_id"]]}]})
            await asyncio.sleep(1.0)
        await asyncio.gather(*ls)
        return sent, row, kept, again

    try:
        sent, row, kept, again = asyncio.run(scenario())
    finally:
        app.state.llm = original
    [run] = sent["task_ids"]
    tab_tasks = {e["task_id"] for e in opened(got["tab"]) if e["tool"] == "testfn.status"}
    assert len(tab_tasks) == 1
    phone_opens = [e for e in opened(got["phone"]) if e["tool"] == "testfn.status"]
    assert len(phone_opens) == 3 and len({e["task_id"] for e in phone_opens}) == 1             # three tries, one panel
    assert len([e for e in got["phone"] if e.get("type") == "screen.check"]) == 3              # each asking for a report
    told = [e for e in got["desk"] if e.get("type") == "sent.shown"]
    assert len(told) == 1 and told[0]["data"]["to"] == "iPhone"                               # shown at last: the sender hears
    assert [e["type"] for e in kept["events"]] == ["panel.open", "panel.result", "panel.close"]
    assert kept["events"][0]["task_id"] == phone_opens[0]["task_id"] and again["events"] == []
    res = json.loads(row["result"]) if isinstance(row["result"], str) else row["result"]
    by = {x["name"]: x for x in res["screens"]}
    assert by["Kitchen tablet"]["on_screen"] and [p["title"] for p in by["Kitchen tablet"]["panels"]] == ["testfn.status"]
    assert by["iPhone"]["panels"] == [] and by["Kitchen tablet"]["role"] == "presentation"
    assert res["headline"].startswith("3 displays")


def test_closing_panels_on_a_display_is_checked_from_its_report(stack):
    """'Close the panels on the kitchen tablet': it closes them and its report says so; a display that
    doesn't answer is said to be unconfirmed, never 'cleared'."""
    from app import auth
    from app.main import app
    orch, ws_url = stack["orch"], stack["orch"].replace("http", "ws") + "/ws"
    sql("UPDATE devices SET status='revoked' WHERE status='approved'")
    auth._by_id.clear()
    tab = device("browser", "Kitchen tablet", {"os": "Windows"}, role="presentation")
    phone = device("browser", "iPhone", {"os": "iPhone"})
    got = {"tab": [], "phone": []}

    async def run(c, args):
        r = (await c.post(f"{orch}/tasks", json={"tool": "constellation.clear", "args": args})).json()
        assert "task_id" in r, r
        tid = r["task_id"]
        for _ in range(150):
            row = (await c.get(f"{orch}/tasks/{tid}")).json()
            if row["status"] != "running":
                return row
            await asyncio.sleep(0.2)

    async def scenario():
        ls = [asyncio.create_task(listen(ws_url, tab, 30, got["tab"])),
              asyncio.create_task(listen(ws_url, phone, 30, got["phone"], report=False))]
        await asyncio.sleep(1.0)
        async with httpx.AsyncClient(timeout=60) as c:
            await c.post(f"{orch}/view/screen", headers={"X-Aurelia-Device": str(tab)}, json={"visible": True, "panels": [
                {"n": 1, "title": "House", "tool": "orbit.status", "tasks": []},
                {"n": 2, "title": "Note", "tool": "constellation.send", "tasks": []}]})
            one = await run(c, {"target": "kitchen tablet", "panels": "2"})
            rep = (await c.post(f"{orch}/tasks", json={"tool": "constellation.screens", "args": {}})).json()
            frozen = await run(c, {"target": "iphone"})
        await asyncio.gather(*ls)
        return one, frozen

    one, frozen = asyncio.run(scenario())
    res = json.loads(one["result"]) if isinstance(one["result"], str) else one["result"]
    assert one["status"] == "completed" and res["headline"] == "Cleared Kitchen tablet"
    clears = [e for e in got["tab"] if e.get("type") == "panels.clear"]
    assert len(clears) == 1 and clears[0]["data"]["panels"] == [2]                      # once: its report confirmed it
    res = json.loads(frozen["result"]) if isinstance(frozen["result"], str) else frozen["result"]
    assert res["headline"] == "Couldn't clear iPhone" and "didn't answer" in res["note"]
    assert len([e for e in got["phone"] if e.get("type") == "panels.clear"]) == 3        # three tries


def test_send_by_fast_path_means_the_same_panel_the_model_would(stack):
    """"send panel 2 to my phone" and "send it to my phone" (no model): the number on this display, or the panel
    he clicked, or the newest one, exactly as when the model sends it."""
    import yaml
    from pathlib import Path
    from app import auth
    from app.config import FastPathPattern
    from app.main import app
    from .conftest import register_testfn
    orch = stack["orch"]
    register_testfn(orch, stack["fn"])                                        # (its registration lapses in a long run)
    sql("UPDATE devices SET status='revoked' WHERE status='approved'")
    auth._by_id.clear()
    desk = device("browser", "Desk", {"os": "Windows", "browser": "Chrome"})
    device("browser", "iPhone", {"os": "iOS", "browser": "Safari", "app": True})
    tasks = []
    for _ in range(2):
        t = httpx.post(f"{orch}/tasks", json={"tool": "testfn.status", "args": {}},
                       headers={"X-Aurelia-Device": str(desk)}).json()["task_id"]
        tasks.append(t)
    time.sleep(1.5)
    screen = {"panels": [{"n": 1, "task_id": tasks[0], "tool": "testfn.status", "title": "Home"},
                         {"n": 2, "task_id": tasks[1], "tool": "testfn.status", "title": "Home again"}], "focus": None}
    real = yaml.safe_load(Path(__file__).parents[4].joinpath("config", "aurelia.yaml").read_text())["fastpath"]["patterns"]
    cfg = app.state.config.current.fastpath
    old = cfg.patterns
    cfg.patterns = [FastPathPattern(**p) for p in real if p["name"].startswith("send-")]
    try:
        def ask(text, scr):
            return httpx.post(f"{orch}/ask", headers={"X-Aurelia-Device": str(desk)}, timeout=30,
                              json={"text": text, "session_id": uuid.uuid4().hex, "screen": scr}).json()

        def sent_panel(out):
            [tid] = out["task_ids"]
            deadline = time.time() + 15
            while (t := httpx.get(f"{orch}/tasks/{tid}").json())["status"] == "running" and time.time() < deadline:
                time.sleep(0.2)
            assert t["status"] == "completed", t["error"]
            return json.loads(t["args"])["_panel_task"]

        out = ask("send panel 1 to my iPhone", screen)
        assert out["route"] == "fastpath" and sent_panel(out) == tasks[0]
        out = ask("send it to my iPhone", screen)                              # the newest one on this screen
        assert sent_panel(out) == tasks[1]
        out = ask("send it to my iPhone", {**screen, "focus": 1})               # the one he clicked
        assert sent_panel(out) == tasks[0]
        out = ask("send panel 5 to my iPhone", screen)
        assert out["route"] == "fastpath" and not out["task_ids"] and out["reply"] == "There's no panel 5 on this screen."
    finally:
        cfg.patterns = old
