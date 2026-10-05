"""Home events read from his words, and what a reported state means (Pulsar, with Lagrange and Orbit);
a connected service's rules (Lagrange)."""
import os

os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")
os.environ.setdefault("REDIS_URL", "redis://unused:6379/0")

import pytest  # noqa: E402

from app import happenings as H  # noqa: E402
from app.governance import GovernanceStore  # noqa: E402


@pytest.mark.parametrize("kind,state,word", [
    ("lock", {"locked": False}, "unlocked"), ("lock", {"locked": True}, "locked"), ("light", {"on": True}, "on"),
    ("leak_sensor", {"active": True}, "wet"), ("thermostat", {"current_temp": 70}, None),
    ("lock", {"state": "unlocked"}, "unlocked"), ("door", {"state": "on"}, "open"), ("door", {"state": "off"}, "closed"),
    ("leak_sensor", {"state": "on"}, "wet"), ("motion_sensor", {"state": "on"}, "motion"),
    ("person", {"state": "not_home"}, "away"), ("person", {"state": "Work"}, "away"), ("person", {"state": "home"}, "home"),
    ("light", {"state": "unavailable"}, None), ("cover", {"state": "opening"}, None),
])
def test_what_a_reported_state_means(kind, state, word):
    assert H.word(kind, state) == word


@pytest.mark.parametrize("text,what,parts", [
    ("when the front door unlocks during quiet hours, turn on the hall light", "turn on the hall light",
     {"dev": "front door", "quiet": True}),
    ("turn on the porch light when I get home", "turn on the porch light", {"me": "home"}),
    ("whenever I leave, lock the front door", "lock the front door", {"me": "away"}),
    ("when the deploy webhook is called, show my releases", "show my releases", {"hook": "deploy"}),
    ("when the sink leak sensor detects water turn off the lights", "turn off the lights", {"dev": "sink leak sensor"}),
    ("turn off the lights when the garage door closes at night", "turn off the lights", {"dev": "garage door", "quiet": True}),
])
def test_home_events_in_his_words(text, what, parts):
    m = H.FIRST.match(text) or H.LAST.match(text)
    assert m and m.group("what") == what
    if "dev" in parts:
        assert m.group("dev").strip() == parts["dev"]
    if "hook" in parts:
        assert m.group("hook") == parts["hook"]
    assert bool(m.group("quiet")) == parts.get("quiet", False)


def test_questions_are_not_events():
    for t in ("when is the door locked", "when do i get home", "what happens when the door unlocks"):
        assert not (H.FIRST.match(t) or H.LAST.match(t)), t


def test_which_device_never_a_guess():
    ents = [{"source": "orbit", "id": "front door", "name": "Front Door", "kind": "lock"},
            {"source": "ha:ha", "id": "lock.front_door", "name": "Front Door", "kind": "lock"},
            {"source": "ha:ha", "id": "lock.back_door", "name": "Back Door", "kind": "lock"},
            {"source": "ha:ha", "id": "binary_sensor.garage", "name": "Garage Door", "kind": "door"}]
    assert H._pick("front door", ents, {"lock"})["source"] == "orbit"      # the same lock in both: mine
    assert H._pick("garage door", ents, {"door"})["id"] == "binary_sensor.garage"
    with pytest.raises(ValueError, match="Which one: Back Door, Front Door"):
        H._pick("door", ents, {"lock"})
    with pytest.raises(ValueError, match="I don't know a lock called “garage”"):
        H._pick("garage", ents, {"lock"})
    people = [{"source": "ha:ha", "id": "person.kentrell", "name": "Kentrell", "kind": "person"},
              {"source": "ha:ha", "id": "person.guest", "name": "Guest", "kind": "person"}]
    assert H._me(people, "Kentrell")["id"] == "person.kentrell"
    with pytest.raises(ValueError, match="connect Home Assistant"):
        H._me([], "Kentrell")


def test_read_back():
    assert H.says({"on": "state", "entity_name": "Front Door", "say": "unlocks", "only": "quiet"}) == \
        "when Front Door unlocks during quiet hours"
    assert H.says({"on": "state", "entity_name": "you", "say": "get home"}) == "when you get home"
    assert H.says({"on": "hook", "hook_name": "Arrive home"}) == "when the Arrive home webhook is called"


def test_a_connected_service_asks_first_until_his_rules_say_otherwise(tmp_path):
    (tmp_path / "functions").mkdir()
    (tmp_path / "charter.yaml").write_text("version: 1\nenforced:\n  default_tier: change\n  tiers:\n    reference: {}\n"
                                           "    act: {}\n    change: {requires_confirmation: true}\n    never: {blocked: true}\n"
                                           "users:\n  owner: {display_name: K, role: owner}\n")
    (tmp_path / "functions" / "orbit.yaml").write_text("function: orbit\ntools:\n  status: {tier: reference}\n")
    g = GovernanceStore(str(tmp_path))
    assert g.decide("gitea", "list_repos", "owner", False).reason.startswith("no policy")     # not connected
    g.connections.add("gitea")
    d = g.decide("gitea", "list_repos", "owner", False)
    assert d.needs_confirmation and d.tier == "change"
    assert g.may_connect("orbit") and g.may_connect("gitea") is None
    (tmp_path / "functions" / "gitea.yaml").write_text("function: gitea\nconnection: true\ntools:\n  list_repos: {tier: reference}\n")
    g.load_all()
    assert g.decide("gitea", "list_repos", "owner", False).allowed
    assert g.decide("gitea", "delete_repo", "owner", False).needs_confirmation
    assert g.may_connect("gitea") is None


def test_only_my_own_network_may_call_me(monkeypatch):
    """I join the home-lab network (Jellyfin): nothing there may call me. The proxy and my services are on my
    own network; my health check comes from inside."""
    import ipaddress
    from fastapi.testclient import TestClient
    from app import peers
    from app.main import app
    monkeypatch.setattr(peers, "stack_network", lambda anchor=peers.ANCHOR: ipaddress.ip_network("172.18.0.0/16"))
    for host, ok in (("172.18.0.7", True), ("127.0.0.1", True), ("172.30.0.4", False)):
        r = TestClient(app, client=(host, 4242)).get("/health")
        assert (r.status_code == 200) is ok, (host, r.status_code)
    r = TestClient(app, client=("172.18.0.7", 4242)).post("/registry/functions", headers={"x-forwarded-host": "a"},
                                                          json={"name": "x", "description": "x", "url": "http://x", "tools": []})
    assert r.status_code == 403                                   # never registered through the proxy
    monkeypatch.setattr(peers, "stack_network", lambda anchor=peers.ANCHOR: None)
    monkeypatch.setenv("STACK_GUARD", "1")
    assert not peers.from_stack("172.18.0.7")                     # can't tell, in the running stack: refused
    monkeypatch.delenv("STACK_GUARD")
    assert peers.from_stack("172.18.0.7")                          # a dev machine or the tests
