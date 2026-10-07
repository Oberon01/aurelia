"""Parallax against a simulated month of Kentrell's use (Central time)."""
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import analysis
from app.analysis import Rules

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 9, 28, 20, 0, tzinfo=TZ).astimezone(timezone.utc)      # a Monday evening
RULES = Rules()


def at(day: int, hh: int, mm: int) -> datetime:
    """`day` days before NOW's date, at hh:mm Central."""
    d = NOW.astimezone(TZ).date() - timedelta(days=day)
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ).astimezone(timezone.utc)


def task(when, tool, tier, args=None, status="completed", ms=800, trigger="user", error=None):
    return {"tool": tool, "args": args or {}, "status": status, "trigger": trigger, "tier": tier,
            "started_at": when, "duration_ms": ms, "error": error}


@pytest.fixture
def month():
    rnd = random.Random(7)
    rows = []
    for day in range(30, -1, -1):
        date = NOW.astimezone(TZ).date() - timedelta(days=day)
        weekday = date.weekday() < 5
        slow = day <= 6                                            # home status got slow this week
        # Weekday mornings: home status between 7:30 and 7:45 (skipped now and then).
        if weekday and rnd.random() < 0.9:
            rows.append(task(at(day, 7, 30 + rnd.randint(0, 14)), "orbit.status", "reference",
                             ms=2400 if slow else 800))
        # Daytime checks at random times, most days.
        for _ in range(rnd.randint(2, 4)):
            rows.append(task(at(day, rnd.randint(9, 21), rnd.randint(0, 59)), "orbit.status", "reference",
                             ms=2400 if slow else 800))
        # Nightly lock around 11 PM; two nights it failed and was retried.
        if rnd.random() < 0.85 and day > 0:
            t = at(day, 22, 55 + rnd.randint(0, 4))
            if day in (5, 12):
                rows.append(task(t, "orbit.lock", "act", {"name": "front door"}, "failed", error="still unlocked"))
                rows.append(task(t + timedelta(minutes=2), "orbit.lock", "act", {"name": "front door"}))
            else:
                rows.append(task(t, "orbit.lock", "act", {"name": "front door"}))
        # Unlock when getting home: a habit, but it always needs confirmation, so never automated.
        if weekday and day > 0:
            rows.append(task(at(day, 18, 5 + rnd.randint(0, 10)), "orbit.unlock", "change", {"name": "front door"}))
        # Lights at random; TV on weekend evenings only.
        rows.append(task(at(day, rnd.randint(8, 22), rnd.randint(0, 59)), "orbit.light", "act",
                         {"name": "living room", "on": True}))
        if not weekday:
            rows.append(task(at(day, 20, rnd.randint(0, 20)), "orbit.tv_power", "act", {"on": True}))
        # The display refreshing a panel on its own isn't a habit.
        rows.append(task(at(day, 7, 31), "chrysalis.releases", "reference", trigger="refresh"))
    return rows


def test_weekday_morning_status_check_becomes_an_automation(month):
    ideas = {p["proposal"]["tool"]: p for p in analysis.recurrences(month, NOW, RULES)}
    s = ideas["orbit.status"]
    assert s["title"].startswith("Check the home status every weekday at 7:")
    assert s["proposal"]["schedule"]["days"] == ["mon", "tue", "wed", "thu", "fri"]
    hh, mm = map(int, s["proposal"]["schedule"]["time"].split(":"))
    assert (hh, 25) <= (7, mm) <= (7, 35)                      # a little before the usual 7:30-7:45
    assert s["confidence"] >= 0.7 and len(s["evidence"]) == 7
    assert "ready when you ask" in s["summary"] and "runs on schedule from now on" in s["effect"]


def test_nightly_lock_is_proposed_with_a_heads_up(month):
    lock = next(p for p in analysis.recurrences(month, NOW, RULES) if p["proposal"]["tool"] == "orbit.lock")
    assert lock["title"] == "Lock front door every day at 10:55 PM"   # usually 10:55-10:59
    assert lock["proposal"]["args"] == {"name": "front door"}
    assert "heads-up you can cancel" in lock["effect"]


def test_no_proposals_from_noise_confirmations_or_the_display(month):
    tools = {p["proposal"]["tool"] for p in analysis.recurrences(month, NOW, RULES)}
    assert "orbit.unlock" not in tools                         # needs confirmation: never scheduled
    assert "orbit.light" not in tools                          # random times
    assert "orbit.tv_power" not in tools                       # weekends only: too few days
    assert "chrysalis.releases" not in tools                   # refreshes aren't habits


def test_too_little_history_proposes_nothing(month):
    recent = [r for r in month if NOW - r["started_at"] < timedelta(days=3)]
    assert analysis.recurrences(recent, NOW, RULES) == []


def test_criticality_from_frequency_retries_and_physical_actions(month):
    ideas = {p["proposal"]["tool"]: p for p in analysis.criticality(month, NOW, RULES, {})}
    assert ideas["orbit.lock"]["proposal"]["level"] == "critical"          # acts on the home; failures retried
    assert any("tried again within 5 minutes 2 times" in e for e in ideas["orbit.lock"]["evidence"])
    assert ideas["orbit.status"]["proposal"]["level"] == "high"            # frequent; you wait on it
    assert "orbit.unlock" in ideas                                          # always confirmed = important
    assert "orbit.light" not in ideas                                       # once a day at random: not "high"
    # Already approved at that level (or higher): not proposed again.
    assert "orbit.lock" not in {p["proposal"]["tool"] for p in
                                analysis.criticality(month, NOW, RULES, {"orbit.lock": "critical"})}


def test_retention_for_frequent_lookups_unless_already_critical(month):
    ideas = analysis.retention(month, NOW, RULES, kept={}, critical=set())
    assert [p["proposal"] for p in ideas] == [{"tool": "orbit.status", "hot_days": 30}]
    assert analysis.retention(month, NOW, RULES, kept={}, critical={"orbit.status"}) == []
    assert analysis.retention(month, NOW, RULES, kept={"orbit.status": 30}, critical=set()) == []


def test_drift_notices_the_slowdown(month):
    d = analysis.drift(month, NOW, RULES)
    assert [x["tool"] for x in d] == ["orbit.status"] and d[0]["ratio"] == 3.0
    assert d[0]["text"] == "Home status checks took 3.0× longer this week (typically 2.4 s vs 800 ms)."


def test_digest_is_quiet_unless_something_needs_you(month):
    quiet_rows = [r for r in month if r["status"] == "completed"]
    quiet = analysis.digest(quiet_rows, NOW, RULES, waiting=[], releases=[], anomalies=[])
    assert quiet["quiet"] is True and quiet["pattern"].startswith("Most used this week: home status checks")
    busy = analysis.digest(month + [task(NOW - timedelta(hours=2), "orbit.lock", "act", status="failed",
                                         error="jammed")], NOW, RULES,
                           waiting=[{"id": 1, "title": "Lock front door every day at 10:55 PM", "kind": "automation"}],
                           releases=[{"sha": "a1b2c3d4e5", "message": "Phase 4\n\nbody", "status": "live"}],
                           anomalies=analysis.drift(month, NOW, RULES))
    assert busy["quiet"] is False and busy["waiting_count"] == 1
    assert busy["failures"] == [{"what": "Locking the door", "count": 1, "last_error": "jammed"}]
    assert busy["automatic"] == [{"what": "Release a1b2c3d4: Phase 4", "outcome": "live"}]
    assert busy["date"] == "Monday, September 28"


def test_rules_come_from_the_policy():
    r = Rules.from_policy({"timezone": "America/New_York", "recurrence": {"min_days": 3}, "retention": {"hot_days": 14}})
    assert (r.timezone, r.min_days, r.retention_days, r.window_days) == ("America/New_York", 3, 14, 14)


def test_a_computer_with_a_problem_is_worth_a_digest(month):
    quiet_rows = [r for r in month if r["status"] == "completed"]
    d = analysis.digest(quiet_rows, NOW, RULES, waiting=[], releases=[], anomalies=[],
                        computers=[{"name": "TV PC", "issues": [{"level": "warn", "text": "Disk / is 92% full (8 GB free)"}]},
                                   {"name": "Laptop", "issues": []}])
    assert d["quiet"] is False and d["computers"] == ["TV PC: Disk / is 92% full (8 GB free)"]
    assert analysis.digest(quiet_rows, NOW, RULES, waiting=[], releases=[], anomalies=[])["computers"] == []


def test_what_pulsar_noticed_and_his_automations_are_in_the_digest(month):
    quiet_rows = [r for r in month if r["status"] == "completed"]
    d = analysis.digest(quiet_rows, NOW, RULES, waiting=[], releases=[], anomalies=[],
                        notices=[{"title": "Front Door is unlocked", "severity": "critical", "status": "open"},
                                 {"title": "TV PC is down", "severity": "high", "status": "cleared"}])
    assert d["quiet"] is False
    assert d["notices"] == [{"title": "Front Door is unlocked", "severity": "critical"},
                            {"title": "TV PC is down", "severity": "high", "cleared": True}]
    ran = analysis.digest(quiet_rows + [task(NOW - timedelta(hours=1), "orbit.lock", "act", trigger="automation"),
                                        task(NOW - timedelta(hours=3), "orbit.light", "act", trigger="schedule")],
                          NOW, RULES, waiting=[], releases=[], anomalies=[])
    assert ran["quiet"] is False and len(ran["automatic"]) == 2


def test_today_on_his_calendars_and_important_email_are_in_the_digest(month):
    quiet_rows = [r for r in month if r["status"] == "completed"]
    from zoneinfo import ZoneInfo
    chi = ZoneInfo(RULES.timezone)
    nine = NOW.astimezone(chi).replace(hour=9, minute=30)
    d = analysis.digest(quiet_rows, NOW, RULES, waiting=[], releases=[], anomalies=[],
                        calendar=[{"title": "Trip to Austin", "starts_at": nine, "all_day": True, "location": "", "source": "mine"},
                                  {"title": "Standup", "starts_at": nine, "all_day": False, "location": "Zoom", "source": "google"}],
                        email=[{"from_name": "Sam Lee", "from_addr": "sam@example.com", "subject": "Lunch?",
                                "why": "someone you've written to"}])
    assert d["quiet"] is False
    assert d["calendar"] == [{"title": "Trip to Austin", "when": "all day", "location": "", "google": False},
                             {"title": "Standup", "when": "9:30 AM", "location": "Zoom", "google": True}]
    assert d["email"] == [{"from": "Sam Lee", "subject": "Lunch?", "why": "someone you've written to"}] and d["email_count"] == 1
    plain = analysis.digest(quiet_rows, NOW, RULES, waiting=[], releases=[], anomalies=[])
    assert plain["calendar"] == [] and plain["email"] == []
    only_cal = analysis.digest(quiet_rows, NOW, RULES, waiting=[], releases=[], anomalies=[],
                               calendar=[{"title": "Dentist", "starts_at": nine, "all_day": False, "location": "", "source": "mine"}])
    assert only_cal["quiet"] is False                                   # a day with something on it isn't quiet
