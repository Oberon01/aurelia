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
    assert (H.conditions(m.group("cond")).get("only") == "quiet") == parts.get("quiet", False)


@pytest.mark.parametrize("text,what,cond", [
    ("when the front door unlocks between 5pm and 6pm, run coming home", "run coming home",
     {"window": {"from": "17:00", "to": "18:00"}}),
    ("when the front door unlocks between 5 pm and 6 p.m. send my youtube to my phone", "send my youtube to my phone",
     {"window": {"from": "17:00", "to": "18:00"}}),
    ("when the front door unlocks after 9pm on weekdays, turn on the hall light", "turn on the hall light",
     {"window": {"from": "21:00", "to": None}, "days": ["mon", "tue", "wed", "thu", "fri"]}),
    ("turn on the porch light when I get home before 7am", "turn on the porch light", {"window": {"from": None, "to": "07:00"}}),
    ("when the front door unlocks on mondays and fridays, turn on the light", "turn on the light", {"days": ["mon", "fri"]}),
    ("when the front door unlocks from 10:30pm to 2am, turn on the light", "turn on the light",
     {"window": {"from": "22:30", "to": "02:00"}}),
    ("when the front door unlocks during quiet hours on weekends, turn on the light", "turn on the light",
     {"only": "quiet", "days": ["sat", "sun"]}),
    ("when the front door unlocks between noon and midnight, turn on the light", "turn on the light",
     {"window": {"from": "12:00", "to": "00:00"}}),
    ("when the front door unlocks, turn on the light", "turn on the light", {}),
    ("when the tv turns on, monitor the lights", "monitor the lights", {}),          # "on mon…" only as days
    ("when the front door opens after 10 minutes, turn on the light", "after 10 minutes, turn on the light", {}),
])
def test_when_an_event_automation_may_run(text, what, cond):
    m = H.FIRST.match(text) or H.LAST.match(text)
    assert m and m.group("what") == what, (m and m.groupdict())
    assert H.conditions(m.group("cond")) == cond


@pytest.mark.parametrize("said,ask", [
    ("between 5 and 6", "Do you mean 5 in the morning or the evening?"),        # a bare hour: asked, never guessed
    ("after 9", "Do you mean 9 in the morning or the evening?"),
    ("between 5pm and 6pm after 7pm", "One time window"),
    ("between 5pm and 5pm", "starts and ends at the same time"),
    ("on weekdays on mondays", "Say the days once"),
    ("after midnight", "until when"), ("before 12am", "until when"),
    ("on weeknights", "Which hours do you mean by weeknights"),
    ("between 12 and 1pm", "noon or 12 midnight"),
    ("after 1am", "until when"), ("after 12:30am", "until when"),           # "after 1am" alone runs nearly all day
])
def test_unclear_times_are_asked_about(said, ask):
    with pytest.raises(ValueError, match=ask):
        H.conditions(" " + said)


def test_a_window_and_days_are_checked_in_his_time_zone(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app import engine, pulsar
    monkeypatch.setattr(engine, "tz_name", lambda app: "America/Chicago")
    monkeypatch.setattr(pulsar, "is_quiet", lambda app, at=None: False)
    at = lambda y, mo, d, h, mi: datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("America/Chicago"))
    five_six = {"window": {"from": "17:00", "to": "18:00"}}
    assert H.allowed(None, five_six, at(2026, 10, 5, 17, 0))
    assert H.allowed(None, five_six, at(2026, 10, 5, 17, 59))
    assert not H.allowed(None, five_six, at(2026, 10, 5, 18, 0))                # the end is not in it
    assert not H.allowed(None, five_six, at(2026, 10, 5, 16, 59))
    assert H.allowed(None, five_six, at(2026, 10, 5, 17, 30))
    from datetime import timezone
    assert H.allowed(None, five_six, datetime(2026, 10, 5, 22, 30, tzinfo=timezone.utc))     # 5:30 PM in Chicago
    after9 = {"window": {"from": "21:00", "to": None}}
    assert H.allowed(None, after9, at(2026, 10, 5, 23, 59)) and not H.allowed(None, after9, at(2026, 10, 6, 0, 30))
    before7 = {"window": {"from": None, "to": "07:00"}}
    assert H.allowed(None, before7, at(2026, 10, 5, 6, 59)) and not H.allowed(None, before7, at(2026, 10, 5, 7, 0))
    # Overnight on Fridays: 1 AM Saturday belongs to Friday's window; 1 AM Friday to Thursday's.
    night_fri = {"window": {"from": "22:00", "to": "02:00"}, "days": ["fri"]}
    assert H.allowed(None, night_fri, at(2026, 10, 9, 23, 0))                   # Friday 11 PM
    assert H.allowed(None, night_fri, at(2026, 10, 10, 1, 0))                   # Saturday 1 AM
    assert not H.allowed(None, night_fri, at(2026, 10, 9, 1, 0))                # Friday 1 AM: Thursday's night
    weekdays = {"days": ["mon", "tue", "wed", "thu", "fri"]}
    assert H.allowed(None, weekdays, at(2026, 10, 5, 12, 0)) and not H.allowed(None, weekdays, at(2026, 10, 4, 12, 0))
    monkeypatch.setattr(pulsar, "is_quiet", lambda app, at=None: True)
    monkeypatch.setattr(pulsar, "quiet_spec", lambda app: "23:00-07:00")
    assert not H.allowed(None, {"only": "quiet", **five_six}, at(2026, 10, 5, 12, 0))       # every condition holds
    assert H.allowed(None, {"only": "quiet"}, at(2026, 10, 5, 12, 0))
    # "at night on Fridays": Friday night's quiet hours, into Saturday morning; never Thursday's night.
    fri_nights = {"only": "quiet", "days": ["fri"]}
    assert H.allowed(None, fri_nights, at(2026, 10, 9, 23, 30))                 # Friday 11:30 PM
    assert H.allowed(None, fri_nights, at(2026, 10, 10, 1, 0))                  # Saturday 1 AM
    assert not H.allowed(None, fri_nights, at(2026, 10, 9, 3, 0))               # Friday 3 AM: Thursday's night


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
    assert H.says({"on": "state", "entity_name": "Front Door", "say": "unlocks", "window": {"from": "17:00", "to": "18:00"}}) == \
        "when Front Door unlocks between 5:00 PM and 6:00 PM"
    assert H.says({"on": "state", "entity_name": "Front Door", "say": "unlocks", "window": {"from": "21:00", "to": None},
                   "days": ["mon", "tue", "wed", "thu", "fri"]}) == "when Front Door unlocks after 9:00 PM on weekdays"
    assert H.says({"on": "hook", "hook_name": "Arrive home", "window": {"from": None, "to": "07:00"}, "days": ["sat", "sun"]}) == \
        "when the Arrive home webhook is called before 7:00 AM on weekends"
    assert H.says({"on": "state", "entity_name": "you", "say": "get home", "days": ["mon", "fri"], "only": "quiet"}) == \
        "when you get home during quiet hours on Mondays and Fridays"
    from app import engine
    assert engine.repeats({"rule": {"on": "online", "device_name": "Den PC", "window": {"from": "17:00", "to": "18:00"}}}) == \
        "when Den PC comes online between 5:00 PM and 6:00 PM"


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


@pytest.mark.parametrize("text,conditional", [
    ("flip the porch light when the front door unlocks if it's dark", True),
    ("whenever I get home, turn on the tv", True),
    ("turn on the light as soon as the door opens", True),
    ("every time the den pc wakes up open youtube", True),
    ("lock the front door after I leave", True), ("turn on the porch light if the front door opens", True),
    ("turn off the lights once I leave", True), ("turn off the tv until I get home", True),
    ("put frieren on the tv after I get home", True), ("next time the door opens, turn on the light", True),
    ("when is the next release", False), ("when does my cap reset", False), ("when did I last lock the door", False),
    ("check if the front door is locked", False), ("tell me if the door is locked", False),
    ("ask me again before restarting the desk", False), ("play it once", False),
    ("turn on the porch light", False), ("play the video", False),
    ("turn on the porch light if its dark", True), ("lock the door if Sarah leaves", True),
    ("turn off the lights once everyone leaves", True), ("turn off the tv unless I'm watching", True),
    ("turn on the light in case I'm late", True), ("turn on the porch light after sunset", True),
    ("lock the front door during quiet hours", True),
])
def test_a_condition_is_never_a_command_now(text, conditional):
    from app import assistant
    assert (assistant._CONDITIONAL.search(text) is not None) == conditional


def test_an_early_after_and_a_count_of_minutes():
    assert H.conditions(" after 5am") == {"window": {"from": "05:00", "to": None}}
    assert H.conditions(" after 10 minutes") == {}                      # a wait, never "after 1"


@pytest.mark.parametrize("what,stray", [
    ("turn on the hall light after 9pm", "after 9pm"), ("play jazz between 5pm and 6pm", "between 5pm and 6pm"),
    ("turn on the lights on weekends", "on weekends"), ("lock the door during quiet hours", "during quiet hours"),
    ("turn off the tv after 10 minutes", None), ("remind me about the weekend plans", None),
    ("send my youtube to my phone", None), ("run coming home", None),
])
def test_a_condition_inside_what_to_do_is_found(what, stray):
    """It would never limit when the automation runs (it's read as part of the command), so it's asked about."""
    assert H.condition_in(what) == stray


def test_a_kept_look_up_is_neither_done_nor_a_failure():
    from app import admiral
    kept = {"headline": "My feed kept for Kentrell's 16 Pro: it shows there when it's opened",
            "kept": {"what": "look-up", "for": ["Kentrell's 16 Pro"]}}
    assert admiral.kept(kept).startswith("My feed kept for") and admiral.unconfirmed(kept) is None
    assert admiral.kept({"headline": "On Kentrell's 16 Pro", "sent": {"confirmed": ["x"]}}) is None
    steps = [{"n": 1, "status": "kept", "depends": []}, {"n": 2, "status": "pending", "depends": [1]}]
    assert [s["n"] for s in admiral.ready(steps)] == [2]                 # the TV still turns on
    assert admiral.blocked(steps) == []
