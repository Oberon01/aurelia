"""After a restart she watches for the computer to come back, and says so (or files an urgent card). The watch
is a job on Admiral's engine: each look is one step, so it carries on across her own restarts."""
import asyncio
import os

os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")
os.environ.setdefault("REDIS_URL", "redis://localhost")

from app import constellation as C  # noqa: E402


def run_watch(monkeypatch, kind, states, booted=111, wait=60):
    """states: what each 5-second look sees, as (online, booted_at). Steps run until the watch settles."""
    told, alerts, seq = [], [], list(states)
    clock = {"t": 1000.0}
    monkeypatch.setattr(C.time, "time", lambda: clock["t"])
    monkeypatch.setattr(C, "BACK_WAIT", wait)
    current = {"v": seq[0]}

    async def online(app, nid): return current["v"][0]
    async def row(nid): return {"id": nid, "name": "TV PC", "info": {"booted_at": current["v"][1]}}
    async def tell(app, user, display, headline, text): told.append(headline)
    async def alert(redis, node, urgent, advice=None): alerts.append((urgent[0]["text"], advice))
    monkeypatch.setattr(C.nodes, "online", online)
    monkeypatch.setattr(C, "_node_row", row)
    monkeypatch.setattr(C, "_tell", tell)
    monkeypatch.setattr(C.nodes, "_alert", alert)
    app = type("A", (), {"state": type("S", (), {"redis": None})()})()
    st = {"user": "owner", "node": {"id": 3, "name": "TV PC"}, "display": 7, "kind": kind, "booted": booted,
          "started": clock["t"], "went_away": False}
    looks = 0

    async def go():
        nonlocal st, looks
        while True:
            clock["t"] += C.WATCH_EVERY                     # the engine runs each look WATCH_EVERY seconds apart
            current["v"] = seq.pop(0) if seq else current["v"]
            looks += 1
            out = await C.watch_step(app, st)
            if out is None:
                return
            assert out["again_in"] == C.WATCH_EVERY
            st = out["spec"]
            assert looks < 200
    asyncio.run(go())
    return told, alerts


def test_back_after_a_restart(monkeypatch):
    # Still checking in (same boot) for a while, then gone, then back with a new boot time.
    told, alerts = run_watch(monkeypatch, "restart", [(True, 111), (True, 111), (False, 111), (False, 111), (True, 222)])
    assert told == ["TV PC is back"] and not alerts


def test_an_older_agent_counts_once_it_went_away_and_came_back(monkeypatch):
    told, _ = run_watch(monkeypatch, "restart", [(True, None), (False, None), (True, None)], booted=None)
    assert told == ["TV PC is back"]


def test_never_came_back_goes_to_the_mailbox(monkeypatch):
    told, alerts = run_watch(monkeypatch, "restart", [(False, 111)] * 30, wait=60)
    assert not told and alerts[0][0].startswith("Didn't come back after the restart")
    assert "what's wrong with TV PC" in alerts[0][1]


def test_shutdown_and_sleep_say_when_its_gone(monkeypatch):
    told, _ = run_watch(monkeypatch, "shutdown", [(True, 111), (False, 111)])
    assert told == ["TV PC is off"]
    told, _ = run_watch(monkeypatch, "sleep", [(True, 111)] * 40)
    assert told == ["TV PC is still on"]


def test_the_watch_state_survives_as_plain_json(monkeypatch):
    import json
    st = {"user": "owner", "node": {"id": 3, "name": "TV PC"}, "display": None, "kind": "wake", "booted": None,
          "started": 1.0, "went_away": True}
    assert json.loads(json.dumps(st)) == st                 # what the engine stores between looks
