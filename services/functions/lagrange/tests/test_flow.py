"""Connections end to end against a real Postgres (TEST_DATABASE_URL; skipped in the build image): the connect
form, the vault, connecting, pinned tools and approvals, a tool run with its secrets scrubbed, turning off,
webhooks in and out, Home Assistant, and who may call what. The orchestrator and the outside services are
a fake network."""
import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg
import httpx
import pytest

from app import main as M
from app.store import Store, pool
from app.vault import Vault

DB = os.environ.get("TEST_DATABASE_URL")
HERE = Path(__file__).resolve()
MIGRATION = (HERE.parents[3] / "orchestrator" / "migrations" / "0023_lagrange.sql" if len(HERE.parents) > 3
             else HERE.parent / "_none")                     # the build image has no repo around it
FAKE = str(Path(__file__).with_name("fake_mcp.py"))
pytestmark = pytest.mark.skipif(not DB or not MIGRATION.exists(), reason="needs TEST_DATABASE_URL and the repo")
OWNER = {"x-aurelia-device": "7", "x-aurelia-user": "owner", "x-forwarded-host": "aurelia.ts.net"}


class Net:
    """The orchestrator and the services around it."""

    def __init__(self):
        self.registered: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.hooks: list[dict] = []
        self.states: list[dict] = []
        self.sent: list[dict] = []
        self.ha = {"light.kitchen": "off", "lock.front_door": "locked"}
        self.tokens_seen: list[str] = []
        self.registered_clients: list = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        u, p = req.url, req.url.path
        if u.host == "orchestrator":
            if p == "/registry/functions" and req.method == "POST":
                fn = json.loads(req.content)
                self.registered[fn["name"]] = fn
                return httpx.Response(204)
            if p.startswith("/registry/functions/") and req.method == "DELETE":
                self.deleted.append(p.rsplit("/", 1)[1])
                self.registered.pop(p.rsplit("/", 1)[1], None)
                return httpx.Response(204)
            if p == "/pulsar/hook":
                self.hooks.append(json.loads(req.content))
                return httpx.Response(200, json={"started": 1})
            if p == "/pulsar/state":
                self.states.append(json.loads(req.content))
                return httpx.Response(200, json={})
        if u.host == "mcp.test" and p == "/mcp":
            if req.headers.get("authorization") not in ("Bearer acc1", "Bearer acc2"):
                return httpx.Response(401, headers={"WWW-Authenticate": 'Bearer resource_metadata="https://mcp.test/.well-known/oauth-protected-resource"'})
            m = json.loads(req.content)
            if "id" not in m or "method" not in m:
                return httpx.Response(202)
            self.tokens_seen.append(req.headers["authorization"])
            res = ({"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}} if m["method"] == "initialize" else
                   {"tools": [{"name": "search", "description": "Search.", "inputSchema": {"type": "object"}}]}
                   if m["method"] == "tools/list" else {"content": [{"type": "text", "text": "3 results"}]})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": m["id"], "result": res})
        if u.host == "mcp.test" and p == "/.well-known/oauth-protected-resource":
            return httpx.Response(200, json={"resource": "https://mcp.test/mcp", "authorization_servers": ["https://auth.test"]})
        if u.host == "auth.test":
            if p == "/.well-known/oauth-authorization-server":
                return httpx.Response(200, json={"authorization_endpoint": "https://auth.test/authorize",
                                                 "token_endpoint": "https://auth.test/token",
                                                 "registration_endpoint": "https://auth.test/register"})
            if p == "/register":
                self.registered_clients.append(json.loads(req.content)["redirect_uris"])
                return httpx.Response(201, json={"client_id": "aurelia-1"})
            if p == "/token":
                form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
                if form["grant_type"] == "authorization_code" and form["code"] == "c0de":
                    return httpx.Response(200, json={"access_token": "acc1", "refresh_token": "ref1", "expires_in": 3600})
                if form["grant_type"] == "refresh_token" and form["refresh_token"] == "ref1":
                    return httpx.Response(200, json={"access_token": "acc2", "expires_in": 3600})
                return httpx.Response(400)
        if u.host == "hooks.test":
            self.sent.append({"body": json.loads(req.content), "auth": req.headers.get("x-key")})
            return httpx.Response(200, json={"ok": True})
        if u.host == "ha.test":
            if req.headers.get("authorization") != "Bearer ha-token-1":
                return httpx.Response(401)
            if p == "/api/":
                return httpx.Response(200, json={"message": "API running."})
            if p == "/api/states":
                return httpx.Response(200, json=[{"entity_id": k, "state": v, "attributes": {"friendly_name": k.split(".")[1].replace("_", " ").title()}}
                                                 for k, v in self.ha.items()])
            if p.startswith("/api/states/"):
                k = p.rsplit("/", 1)[1]
                return httpx.Response(200, json={"entity_id": k, "state": self.ha[k], "attributes": {
                    "friendly_name": k.split(".")[1].replace("_", " ").title()}})
            if p.startswith("/api/services/light/turn_on"):
                self.ha[json.loads(req.content)["entity_id"]] = "on"
                return httpx.Response(200, json=[])
        return httpx.Response(404)


class Ctx:
    def __init__(self, args: dict, task_id: str = "t1"):
        self.task_id, self.args = task_id, args
        self.progressed, self.out, self.error = [], None, None
        self.cancelled = False

    async def progress(self, cur, total, unit, label=None):
        self.progressed.append((cur, total, label))

    async def result(self, data, status="completed", partial=False):
        self.out = data

    async def fail(self, error):
        self.error = error


async def run(fn, args: dict) -> Ctx:
    ctx = Ctx(args)
    try:
        await fn(ctx)
    except Exception as exc:
        ctx.error = str(exc)
    return ctx


async def tool(name: str, tool_name: str, args: dict) -> Ctx:
    return await run(lambda c: M.run_conn_tool(c, name, tool_name), args)


@pytest.fixture
async def env(tmp_path, monkeypatch):
    conn = await asyncpg.connect(DB)
    try:
        await conn.execute(MIGRATION.read_text())
        await conn.execute("DELETE FROM connections")
    finally:
        await conn.close()
    net = Net()
    M.S.update(store=Store(await pool(DB), Vault(os.urandom(32))), transport=httpx.MockTransport(net),
               sessions={}, registered=set(), entities={}, watchers={}, hook_calls={})
    changed = {"on": False}

    def argv(conn):
        return [sys.executable, FAKE, *(["--changed"] if changed["on"] else [])], str(tmp_path / conn["name"])
    monkeypatch.setattr(M, "local_argv", argv)
    monkeypatch.setattr(M, "_uid", lambda conn: None)                       # (the sandbox has its own test)
    monkeypatch.setattr(M, "keep_watchers", _no_watchers)
    asgi = httpx.AsyncClient(transport=httpx.ASGITransport(app=M.app), base_url="http://lagrange:9000")
    yield net, asgi, changed
    await asgi.aclose()
    for s in list(M.S["sessions"].values()):
        await s.close()
    await M.S["store"].db.close()
    M.S.update(store=None, transport=None, sessions={})


async def _no_watchers():
    return None


async def draft(asgi, **body):
    r = await asgi.post("/lagrange/drafts", json={"origin": "https://aurelia.ts.net", **body}, headers=OWNER)
    return r.status_code, r.json()


async def test_a_local_server_from_the_form_to_its_first_use(env):
    net, asgi, changed = env
    code, out = await draft(asgi, label="Gitea", kind="mcp_local", package="npm:gitea-mcp@1.2.0",
                            env=[{"name": "SECRET_THING", "value": "xyzzy-123", "secret": True},
                                 {"name": "GITEA_HOST", "value": "http://gitea:3000"}])
    assert code == 200 and out["status"] == "ready", out
    assert [t["name"] for t in out["tools"]] == ["list_repos", "create_issue", "env"] and out["tools"][0]["read_only"]
    rows = await M.store().db.fetch("SELECT slot, box FROM connection_secrets")
    assert [r["slot"] for r in rows] == ["env:SECRET_THING"] and b"xyzzy" not in bytes(rows[0]["box"])
    assert "gitea" not in net.registered                                   # a draft registers nothing

    c = await run(M.t_connect, {"name": "gitea"})
    assert c.error is None and c.out["connection"]["status"] == "connected"
    assert c.out["headline"] == "Connected Gitea: 3 tools, each asks you first"
    fn = net.registered["gitea"]
    assert fn["via"] == "lagrange" and fn["url"] == "http://lagrange:9000/c/gitea"
    assert [t["name"] for t in fn["tools"]] == ["list_repos", "create_issue", "env"]
    assert "only reads" in fn["tools"][0]["description"]
    assert "connection: true" in c.out["connection"]["rule_file"] and "list_repos: { tier: change }" in c.out["connection"]["rule_file"]

    r = await tool("gitea", "list_repos", {"owner": "kentrell", "_display": "panel"})
    assert r.error is None and r.out["output"]["text"].endswith("(kentrell)") and r.out["output"]["structured"]["repos"]
    assert (1.0, 2.0, "halfway") in r.progressed
    r = await tool("gitea", "env", {"name": "SECRET_THING"})
    assert r.out["output"]["text"] == "[secret]"                          # a secret never leaves Lagrange
    r = await tool("gitea", "create_issue", {"title": "x"})
    assert r.error == "Gitea said: no permission [secret]"                   # an error quoting a secret: cut out
    r = await tool("gitea", "delete_repo", {})
    assert "no tool" in r.error

    changed["on"] = True                                                   # the server changes its tools
    f = await run(M.t_refresh, {"name": "gitea"})
    assert f.out["headline"] == "Gitea: 2 new or changed, waiting for your OK"
    assert {t["name"] for t in f.out["connection"]["pending"]} == {"list_repos", "delete_repo"}
    assert [t["name"] for t in net.registered["gitea"]["tools"]] == ["create_issue", "env"]   # held ones aren't offered
    v = await run(M.t_connection, {"name": "gitea"})                      # its panel: counted in the headline, listed below
    assert v.out["headline"] == "Gitea: 2 tools · 2 waiting for your OK", v.out["headline"]
    assert [t["name"] for t in v.out["connection"]["tools"]] == ["create_issue", "env"]
    a = await run(M.t_approve, {"name": "gitea"})
    assert a.error is None and sorted(t["name"] for t in net.registered["gitea"]["tools"]) == \
        ["create_issue", "delete_repo", "env", "list_repos"]
    assert (await run(M.t_connection, {"name": "gitea"})).out["headline"] == "Gitea: 4 tools"
    assert (await run(M.t_approve, {"name": "gitea"})).error.startswith("Gitea has no tools waiting")

    off = await run(M.t_turn_off, {"name": "gitea"})
    assert off.error is None and "gitea" in net.deleted and "gitea" not in net.registered
    assert "isn't connected" in (await tool("gitea", "list_repos", {})).error
    await run(M.t_turn_on, {"name": "gitea"})
    assert "gitea" in net.registered
    lst = await run(M.t_connections, {})
    assert lst.out["connections"][0]["label"] == "Gitea" and lst.out["headline"] == "1 connection"

    gone = await run(M.t_disconnect, {"name": "the gitea connection"})
    assert gone.error is None and "gitea" not in net.registered
    assert await M.store().db.fetchval("SELECT count(*) FROM connection_secrets") == 0


async def test_the_form_is_his_alone_and_names_stay_mine(env):
    net, asgi, _ = env
    r = await asgi.post("/lagrange/drafts", json={"label": "X", "kind": "hook_in"})
    assert r.status_code == 403
    r = await asgi.post("/lagrange/drafts", json={"label": "X", "kind": "hook_in"},
                        headers={"x-aurelia-device": "3", "x-aurelia-user": "guest"})
    assert r.status_code == 403
    code, out = await draft(asgi, label="Orbit", kind="hook_in")
    assert code == 400 and "my own names" in out["error"]
    code, out = await draft(asgi, label="Thing", kind="mcp_local", package="npm:thing")
    assert code == 400 and "exact version" in out["error"]
    code, out = await draft(asgi, label="Thing", kind="mcp_http", url="http://user:pw@x.test/mcp")
    assert code == 400 and "own field" in out["error"]
    r = await asgi.post("/tools/connections/invoke", json={"task_id": "x", "args": {}}, headers={"x-forwarded-host": "a"})
    assert r.status_code == 403                                            # never through the proxy
    assert (await asgi.get("/ha/entities", headers=OWNER)).status_code == 403


async def test_webhooks_in_and_out(env):
    net, asgi, _ = env
    code, out = await draft(asgi, label="Arrive home", kind="hook_in")
    path = out["hook_path"]
    assert code == 200 and path.startswith("/hooks/arrive_home/") and len(path) > 40
    assert (await asgi.post(path, json={"who": "me"})).status_code == 409   # not connected yet: a draft
    await run(M.t_connect, {"name": "arrive home"})
    assert "arrive_home" not in net.registered                            # a webhook in has no tools
    r = await asgi.post(path, json={"who": "me"})
    assert r.status_code == 200 and r.json()["heard"] == "Arrive home"
    assert net.hooks == [{"hook": "arrive_home", "label": "Arrive home", "data": {"who": "me"}}]
    assert (await asgi.post(path[:-3] + "xyz", json={})).status_code == 404
    assert (await asgi.get(f"/lagrange/hooks/arrive_home/url", headers=OWNER)).json()["path"] == path
    assert (await asgi.get(f"/lagrange/hooks/arrive_home/url")).status_code == 403

    code, out = await draft(asgi, label="Notify", kind="hook_out", url="https://hooks.test/in/T0KENINPATH-123456",
                            header_name="X-Key", header_value="hook-secret-9")
    assert code == 200 and out["tools"][0]["name"] == "send" and out["connection"]["where"] == "hooks.test"
    cfg = await M.store().db.fetchval("SELECT config FROM connections WHERE name='notify'")
    assert "T0KENINPATH" not in json.dumps(cfg)                            # an address's path stays in the vault
    await run(M.t_connect, {"name": "notify"})
    r = await tool("notify", "send", {"message": "dinner at 7"})
    assert r.error is None and r.out["headline"] == "Sent to Notify"
    assert net.sent == [{"body": {"from": "aurelia", "message": "dinner at 7"}, "auth": "hook-secret-9"}]


async def test_home_assistant_from_the_form_to_a_checked_action(env):
    net, asgi, _ = env
    code, out = await draft(asgi, label="Home Assistant", kind="home_assistant", url="http://ha.test:8123", token="wrong")
    assert code == 400 and "token" in out["error"]
    code, out = await draft(asgi, label="Home Assistant", kind="home_assistant", url="http://ha.test:8123", token="ha-token-1")
    assert code == 200 and out["entities"] == 2 and [t["name"] for t in out["tools"]] == ["states", "call"]
    await run(M.t_connect, {"name": "home assistant"})
    assert [t["name"] for t in net.registered["home_assistant"]["tools"]] == ["states", "call"]
    s = await tool("home_assistant", "states", {"query": "kitchen"})
    assert s.out["entities"] == [{"id": "light.kitchen", "name": "Kitchen", "kind": "light", "state": "off"}]
    c = await tool("home_assistant", "call", {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"})
    assert c.error is None and c.out["output"]["done"] and "reports Kitchen on" in c.out["headline"]
    bad = await tool("home_assistant", "call", {"domain": "light", "service": "turn_on", "entity_id": "kitchen; rm"})
    assert "isn't an entity id" in bad.error
    ents = (await asgi.get("/ha/entities")).json()["entities"]
    assert {e["id"] for e in ents} == {"light.kitchen", "lock.front_door"} and ents[0]["source"] == "ha:home_assistant"


async def test_a_server_that_wants_a_sign_in(env):
    from urllib.parse import parse_qs, urlsplit
    net, asgi, _ = env
    code, out = await draft(asgi, label="Remote", kind="mcp_http", url="https://mcp.test/mcp")
    assert code == 200 and out["status"] == "needs_signin", out
    q = parse_qs(urlsplit(out["sign_in"]).query)
    assert q["redirect_uri"] == ["https://aurelia.ts.net/lagrange/oauth/callback"] and q["code_challenge_method"] == ["S256"]
    assert net.registered_clients == [["https://aurelia.ts.net/lagrange/oauth/callback"]]
    c = await run(M.t_connect, {"name": "remote"})
    assert "Sign in to Remote first" in c.error
    page = await asgi.get("/lagrange/oauth/callback", params={"state": q["state"][0], "code": "c0de"})
    assert page.status_code == 200 and "Signed in to Remote: 1 tool" in page.text
    again = await asgi.get("/lagrange/oauth/callback", params={"state": q["state"][0], "code": "c0de"})
    assert again.status_code == 400                                         # one use only
    assert (await run(M.t_connect, {"name": "remote"})).error is None
    rows = await M.store().db.fetch("SELECT box FROM connection_secrets")
    assert all(b"acc1" not in bytes(r["box"]) for r in rows)
    saved = await M.store().oauth((await M.store().get("remote"))["id"])
    await M.store().put_oauth((await M.store().get("remote"))["id"], {**saved, "expires_at": 0})
    r = await tool("remote", "search", {})
    assert r.error is None and r.out["output"]["text"] == "3 results" and net.tokens_seen[-1] == "Bearer acc2"


async def test_only_my_stack_may_call_and_local_servers_are_fenced(env, monkeypatch):
    import ipaddress
    from app import peers
    net, asgi, _ = env
    monkeypatch.setattr(peers, "stack_network", lambda anchor=peers.ANCHOR: ipaddress.ip_network("172.18.0.0/16"))
    for host, ok in (("172.18.0.5", True), ("172.20.0.9", False), ("127.0.0.1", False)):
        t = httpx.ASGITransport(app=M.app, client=(host, 5555))
        async with httpx.AsyncClient(transport=t, base_url="http://lagrange:9000") as c:
            r = await c.post("/tools/connections/invoke", json={"task_id": "x", "args": {}})
            assert (r.status_code == 202) is ok, (host, r.status_code)       # home lab and loopback: refused
            r = await c.post("/lagrange/drafts", json={"label": "X", "kind": "hook_in"}, headers=OWNER)
            assert (r.status_code != 403) is ok, (host, r.status_code)
    rules = M.firewall_rules(["172.18.0.0/16", "172.30.0.0/16"])
    assert ["OUTPUT", "-m", "owner", "--uid-owner", "20000-28999", "-o", "lo", "-j", "REJECT"] in rules
    for n in ("172.18.0.0/16", "172.30.0.0/16"):                          # my stack, and the home-lab network
        assert ["OUTPUT", "-m", "owner", "--uid-owner", "20000-28999", "-d", n, "-j", "REJECT"] in rules
    assert rules[0][-3:] == ["127.0.0.11", "-j", "ACCEPT"]                 # Docker's DNS stays reachable
    monkeypatch.setattr(peers, "stack_network", lambda anchor=peers.ANCHOR: None)
    M.S["firewall"] = False
    with pytest.raises(Exception, match="won't run a local server"):
        await M.ensure_firewall()
