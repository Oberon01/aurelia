from datetime import datetime, timezone

import pytest

from app.inspect import ago, detail, find_name, health_note, overview

NOW = datetime(2026, 9, 28, 20, 0, tzinfo=timezone.utc)
REGISTERED = [
    {"name": "orbit", "description": "Smart home.", "url": "http://orbit:9000",
     "tools": [{"name": "status", "description": "State of everything."}, {"name": "lock", "description": "Lock a door."}]},
    {"name": "probe", "description": "Diagnostics.", "url": "http://probe:9000",
     "tools": [{"name": "echo", "description": "Echo."}]},
]
POLICIES = [
    {"function": "orbit", "enabled": True, "tools": {"status": "reference", "lock": "act"}, "rate_limit_per_minute": 30},
    {"function": "probe", "enabled": True, "tools": {"echo": "reference"}, "rate_limit_per_minute": 120},
    {"function": "nebula", "enabled": True, "tools": {"search": "read", "download": "act"}, "rate_limit_per_minute": 60},
    {"function": "kuiper", "enabled": True, "tools": {"scan": "read"}, "rate_limit_per_minute": 60},
]
WEEK = [{"tool": "orbit.status", "runs": 12, "failed": 1, "median_ms": 840, "last_run": "2026-09-28T19:57:00+00:00"},
        {"tool": "orbit.lock", "runs": 3, "failed": 0, "median_ms": 3200, "last_run": "2026-09-28T18:00:00+00:00"}]
YEAR = WEEK + [{"tool": "kuiper.scan", "runs": 1, "failed": 1, "median_ms": 10, "last_run": "2026-09-01T00:00:00+00:00"}]
HEALTH = {"orbit": {"status": "ok", "smartrent": "ready", "tvs": [{"name": "Living Room TV", "ip": "10.0.0.5"}]},
          "probe": None}


def test_overview_statuses_counts_and_use():
    seen = {"orbit": "2026-09-28T19:59:50+00:00", "probe": "2026-09-28T19:59:55+00:00", "kuiper": "2026-09-28T17:00:00+00:00"}
    rows = {r["name"]: r for r in overview(REGISTERED, POLICIES, WEEK, YEAR, HEALTH, seen, NOW)}
    assert list(rows) == ["Orbit", "Probe", "Kuiper", "Nebula"]            # running first
    o = rows["Orbit"]
    assert (o["state"], o["tools"], o["runs_7d"], o["failed_7d"], o["last_used"]) == ("online", 2, 15, 1, "3 min ago")
    assert o["health"] == "smartrent ready · tvs Living Room TV"
    assert rows["Probe"]["state"] == "unhealthy"                         # its /health didn't answer
    assert rows["Nebula"]["state"] == "planned" and rows["Nebula"]["tools"] == 2
    assert rows["Kuiper"]["state"] == "offline" and rows["Kuiper"]["last_seen"] == "3 h ago"   # ran before


def test_detail_lists_each_tool_with_tier_and_use():
    d = detail("orbit", REGISTERED, POLICIES, {"policy": {"rules": {"thermostat": {"min_f": 60, "max_f": 85}}}},
               WEEK, YEAR, HEALTH["orbit"], {"orbit": "2026-09-28T19:59:50+00:00"}, NOW)
    assert d["function"] == "Orbit" and d["state"] == "online" and d["rate_limit"] == "30 per minute"
    status = next(t for t in d["tools"] if t["tool"] == "status")
    assert (status["tier"], status["runs_7d"], status["typical"], status["last_used"]) == ("reference", 12, "840 ms", "3 min ago")
    assert d["rules"] == {"thermostat": {"min_f": 60, "max_f": 85}}
    planned = detail("nebula", REGISTERED, POLICIES, None, WEEK, YEAR, None, {}, NOW)
    assert planned["state"] == "planned" and [t["tool"] for t in planned["tools"]] == ["search", "download"]


def test_names_are_forgiving():
    names = ["nebula", "orbit", "probe"]
    assert find_name("Orbit function", names) == "orbit" and find_name("the orbt", names) == "orbit"
    with pytest.raises(LookupError, match="Functions: nebula, orbit, probe"):
        find_name("weather", names)


def test_small_helpers():
    assert ago(None) is None and ago("2026-09-26T20:00:00+00:00", NOW) == "2 d ago"
    assert health_note({"status": "ok"}) is None
