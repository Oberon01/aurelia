"""Beacon's days, read with my own parser: 'today', 'thursday', 'this week', 'next week', 'the weekend', 'oct 8'."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app import beacon_glue as B
from app import when

WED = datetime(2026, 10, 7, 21, 0, tzinfo=ZoneInfo("America/Chicago"))       # a Wednesday evening
SUN = datetime(2026, 10, 11, 10, 0, tzinfo=ZoneInfo("America/Chicago"))


@pytest.mark.parametrize("said,first,n", [
    ("", date(2026, 10, 7), 1), ("today", date(2026, 10, 7), 1), ("tomorrow", date(2026, 10, 8), 1),
    ("thursday", date(2026, 10, 8), 1), ("on friday", date(2026, 10, 9), 1), ("this week", date(2026, 10, 7), 7),
    ("next week", date(2026, 10, 12), 7), ("the weekend", date(2026, 10, 10), 2), ("oct 20", date(2026, 10, 20), 1),
])
def test_days(said, first, n):
    assert B.days(said, WED) == (first, n)


def test_on_a_sunday_the_weekend_is_today():
    assert B.days("this weekend", SUN) == (date(2026, 10, 11), 1)


@pytest.mark.parametrize("said", ["whenever", "thursday at 3", "the 5th of never"])
def test_a_day_i_cant_read_is_asked_about(said):
    with pytest.raises(when.Unclear):
        B.days(said, WED)


def test_ntfy_for_defaults_to_critical():
    class Pol:
        rules = {}

    class Gov:
        functions = {"pulsar": Pol()}

    class State:
        governance = Gov()

    class App:
        state = State()
    assert B.ntfy_for(App()) == {"critical"}
    Pol.rules = {"ntfy_for": ["critical", "high"]}
    assert B.ntfy_for(App()) == {"critical", "high"}


def test_a_plan_can_never_use_what_only_his_yes_runs(monkeypatch):
    """Sending an email, saving or removing an event: not in any plan's catalog, the planner's or a skill's."""
    from app import admiral, registry

    class T:
        def __init__(self, n):
            self.name, self.description, self.input_schema = n, n, {}

    class F:
        def __init__(self, name, tools):
            self.name, self.tools = name, [T(t) for t in tools]

    async def fake(redis):
        return [F("beacon", ["inbox", "draft_email", "send", "agenda", "add_event", "save_event", "remove_event", "drop_event"])]
    monkeypatch.setattr(registry, "list_all", fake)
    monkeypatch.setattr(admiral, "tier_of", lambda app, q: "act")

    class App:
        class state:
            redis = None
    import asyncio
    cat = asyncio.run(admiral.catalog(App()))
    assert set(cat) == {"beacon.inbox", "beacon.draft_email", "beacon.agenda", "beacon.add_event", "beacon.remove_event"}
