"""Reading a time from his words, by code: exact, in his time zone, through daylight saving, and refusing
rather than guessing. Plus the engine's next occurrence of a recurring time."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import engine, when

CT = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 3, 18, 31, tzinfo=CT)        # a Saturday evening


@pytest.mark.parametrize("said,expect", [
    ("in 20 minutes", "2026-10-03T18:51"), ("in an hour", "2026-10-03T19:31"), ("in half an hour", "2026-10-03T19:01"),
    ("in 2 days", "2026-10-05T18:31"), ("in 90 seconds", "2026-10-03T18:32"),
    ("at 5", "2026-10-04T17:00"),                     # 1-6 is PM; 5 PM has passed today, so tomorrow
    ("at 9", "2026-10-03T21:00"),                     # 7-11: whichever comes next
    ("at 7:15", "2026-10-03T19:15"), ("at 5:30 pm", "2026-10-04T17:30"), ("at 11 PM", "2026-10-03T23:00"),
    ("at 11 p.m.", "2026-10-03T23:00"), ("at noon", "2026-10-04T12:00"), ("at midnight", "2026-10-04T00:00"),
    ("17:45", "2026-10-04T17:45"), ("tonight at 11", "2026-10-03T23:00"), ("tonight at 12", "2026-10-04T00:00"),
    ("tomorrow at 8", "2026-10-04T08:00"), ("tomorrow morning", "2026-10-04T09:00"),
    ("tomorrow evening at 7", "2026-10-04T19:00"), ("friday at 9", "2026-10-09T09:00"),
    ("this saturday at 9pm", "2026-10-03T21:00"), ("next saturday at 9am", "2026-10-10T09:00"),
    ("on oct 5 at 3pm", "2026-10-05T15:00"), ("October 5th at 3 p.m.", "2026-10-05T15:00"),
])
def test_his_words_become_an_exact_time(said, expect):
    assert when.parse(said, NOW).strftime("%Y-%m-%dT%H:%M") == expect


@pytest.mark.parametrize("said,question", [
    ("5", "Did you mean at 5"), ("later", "can't tell when"), ("tomorrow", "At what time"), ("friday", "At what time"),
    ("at 25", "can't tell when"), ("in 400 days", "more than a year"), ("", "When should I do it"),
])
def test_it_asks_instead_of_guessing(said, question):
    with pytest.raises(when.Unclear, match=question):
        when.parse(said, NOW)


def test_daylight_saving_skipped_and_repeated_times():
    spring = when.parse("tomorrow at 2:30 am", datetime(2027, 3, 13, 12, tzinfo=CT))
    assert (spring.hour, spring.minute) == (3, 0) and spring.utcoffset().total_seconds() == -5 * 3600
    fall = when.parse("tomorrow at 1:30 am", datetime(2026, 10, 31, 12, tzinfo=CT))
    assert (fall.hour, fall.minute) == (1, 30) and fall.utcoffset().total_seconds() == -5 * 3600   # the first 1:30


def test_the_time_and_the_command_are_split_from_one_sentence():
    at, what, words = when.split("at 11 PM, lock the front door.", NOW)
    assert (at.hour, what, words) == (23, "lock the front door", "at 11 PM")
    at, what, _ = when.split("turn off the lights in 20 minutes", NOW)
    assert (at.minute, what) == (51, "turn off the lights")
    at, what, _ = when.split("to call Mom tomorrow at 9", NOW)                # his capitals are kept
    assert (at.day, at.hour, what) == (4, 9, "to call Mom")
    with pytest.raises(when.Unclear):
        when.split("lock the front door", NOW)


def test_times_read_back_plainly():
    assert when.say(datetime(2026, 10, 3, 23, tzinfo=CT), NOW) == "today at 11:00 PM"
    assert when.say(datetime(2026, 10, 4, 8, tzinfo=CT), NOW) == "tomorrow at 8:00 AM"
    assert when.say(datetime(2026, 10, 9, 9, tzinfo=CT), NOW) == "Fri, Oct 9, 9:00 AM"


def test_the_next_occurrence_of_a_recurring_time():
    rule = {"days": ["mon", "wed", "fri"], "time": "07:30"}
    nxt = engine.next_time(rule, NOW, "America/Chicago")
    assert nxt.isoformat() == "2026-10-05T07:30:00-05:00"
    nov = engine.next_time({"days": ["mon"], "time": "07:30"}, datetime(2026, 10, 31, tzinfo=CT), "America/Chicago")
    assert nov.utcoffset().total_seconds() == -6 * 3600                          # after the change, standard time
    every = engine.next_time({"every_minutes": 10}, NOW, "America/Chicago")
    assert (every - NOW).total_seconds() == 600
    assert engine.next_time({"once": True}, NOW, "America/Chicago") is None


@pytest.mark.parametrize("now_h,said,expect", [
    (9, "tonight at 1", "2026-10-04T01:00"),            # the small hours, not 1 PM
    (20, "tonight at 1", "2026-10-04T01:00"),
    (9, "tomorrow night at 12", "2026-10-05T00:00"),    # midnight at the end of tomorrow, not tonight
    (9, "tomorrow night at 1", "2026-10-05T01:00"),
    (9, "tonight at 11", "2026-10-03T23:00"),
    (9, "in a couple of hours", "2026-10-03T11:00"),
    (9, "in a few minutes", "2026-10-03T09:03"),
])
def test_night_hours_and_loose_amounts(now_h, said, expect):
    assert when.parse(said, NOW.replace(hour=now_h, minute=0)).strftime("%Y-%m-%dT%H:%M") == expect


def test_in_two_hours_is_two_real_hours_across_the_clock_change():
    at = when.parse("in 2 hours", datetime(2026, 11, 1, 0, 30, tzinfo=CT))
    utc = ZoneInfo("UTC")                                # (same-zone subtraction in Python counts wall time)
    assert (at.astimezone(utc) - datetime(2026, 11, 1, 0, 30, tzinfo=CT).astimezone(utc)).total_seconds() == 7200
    assert (at.hour, at.minute) == (1, 30) and at.utcoffset().total_seconds() == -6 * 3600   # the second 1:30


@pytest.mark.parametrize("now_h,said,expect", [
    (20, "tonight at midnight", "2026-10-04T00:00"), (9, "tomorrow night at midnight", "2026-10-05T00:00"),
    (9, "tonight at 5", "2026-10-03T17:00"), (9, "in a couple hours", "2026-10-03T11:00"),
])
def test_midnight_at_night_and_the_edge_of_the_small_hours(now_h, said, expect):
    assert when.parse(said, NOW.replace(hour=now_h, minute=0)).strftime("%Y-%m-%dT%H:%M") == expect


# ---- repeating times (Pulsar) ------------------------------------------------------------------------------

WD, ALL = ["mon", "tue", "wed", "thu", "fri"], ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@pytest.mark.parametrize("said,rule,back", [
    ("every day at 7 am", {"days": ALL, "time": "07:00"}, "every day at 7:00 AM"),
    ("daily at 21:00", {"days": ALL, "time": "21:00"}, "every day at 9:00 PM"),
    ("at 7am every day", {"days": ALL, "time": "07:00"}, "every day at 7:00 AM"),
    ("every weekday at 7:30 am", {"days": WD, "time": "07:30"}, "every weekday at 7:30 AM"),
    ("weekends at 10 am", {"days": ["sat", "sun"], "time": "10:00"}, "every weekend day at 10:00 AM"),
    ("every monday and thursday at 6 pm", {"days": ["mon", "thu"], "time": "18:00"}, "every Monday and Thursday at 6:00 PM"),
    ("mondays at 6pm", {"days": ["mon"], "time": "18:00"}, "every Monday at 6:00 PM"),
    ("every tues and thurs at 5pm", {"days": ["tue", "thu"], "time": "17:00"}, "every Tuesday and Thursday at 5:00 PM"),
    ("every night at 11", {"days": ALL, "time": "23:00"}, "every day at 11:00 PM"),
    ("every friday night at 1", {"days": ["sat"], "time": "01:00"}, "every Saturday at 1:00 AM"),   # the small hours after
    ("every morning", {"days": ALL, "time": "09:00"}, "every day at 9:00 AM"),
    ("every weekday morning at 6:45", {"days": WD, "time": "06:45"}, "every weekday at 6:45 AM"),
    ("every 2 hours", {"every_minutes": 120}, "every 2 hours"), ("hourly", {"every_minutes": 60}, "every hour"),
    ("every 30 minutes", {"every_minutes": 30}, "every 30 minutes"), ("every half hour", {"every_minutes": 30}, "every 30 minutes"),
    ("on the 1st of every month at 9 am", {"monthday": 1, "time": "09:00"}, "on the 1st of every month at 9:00 AM"),
    ("every month on the 15th at noon", {"monthday": 15, "time": "12:00"}, "on the 15th of every month at 12:00 PM"),
    ("the last day of every month at 8 pm", {"monthday": 31, "time": "20:00"}, "on the last day of every month at 8:00 PM"),
])
def test_repeating_times_in_his_words(said, rule, back):
    assert when.recurring(said) == rule and when.say_rule(rule) == back


@pytest.mark.parametrize("said,question", [
    ("every day", "At what time?"), ("every weekday at 7", "7 in the morning or 7 at night?"),
    ("every other day at 9 am", "isn't something I can repeat"), ("on the 32nd of every month at 9 am", "doesn't have"),
])
def test_repeating_times_ask_instead_of_guessing(said, question):
    with pytest.raises(when.Unclear, match=question):
        when.recurring(said)


@pytest.mark.parametrize("said", ["monday at 7 am", "tomorrow at 9", "in 20 minutes", "the plants", "tues at 5pm"])
def test_a_single_time_is_not_a_repeating_one(said):
    assert when.recurring(said) is None


def test_the_command_and_the_repeating_time_are_split():
    assert when.split_recurring("every weekday at 7 am lock the front door") == \
        ({"days": WD, "time": "07:00"}, "lock the front door", "every weekday at 7 am")
    assert when.split_recurring("lock the front door every night at 11")[:2] == ({"days": ALL, "time": "23:00"},
                                                                                "lock the front door")
    with pytest.raises(when.Unclear, match="7 in the morning"):
        when.split_recurring("every day at 7 turn on the lights")
    with pytest.raises(when.Unclear, match="how often"):
        when.split_recurring("turn on the lights")


def test_the_next_day_of_the_month_and_its_last_day():
    at = datetime(2026, 1, 31, 10, 0, tzinfo=CT)
    nxt = engine.next_time({"monthday": 31, "time": "08:00"}, at, "America/Chicago")
    assert nxt.astimezone(CT).isoformat()[:16] == "2026-02-28T08:00"           # February's last day
    nxt = engine.next_time({"monthday": 1, "time": "09:00"}, datetime(2026, 12, 2, tzinfo=CT), "America/Chicago")
    assert nxt.astimezone(CT).isoformat()[:16] == "2027-01-01T09:00"
    nxt = engine.next_time({"monthday": 15, "time": "12:00"}, datetime(2026, 3, 14, 23, 0, tzinfo=CT), "America/Chicago")
    assert nxt.astimezone(CT).isoformat()[:16] == "2026-03-15T12:00"
    assert engine.next_time({"on": "online", "device": 3}, NOW, "America/Chicago") is None       # an event's: no clock


def test_an_automation_says_how_it_repeats():
    assert engine.repeats({"rule": {"days": WD, "time": "07:00"}}) == "every weekday at 7:00 AM"
    assert engine.repeats({"rule": {"on": "online", "device_name": "Den PC"}}) == "when Den PC comes online"
    assert engine.repeats({"rule": {"on": "open", "device_name": "Kitchen"}}) == "when Kitchen opens"
    assert engine.repeats({"rule": {"once": True}}) is None
    assert engine.recurring({"created_by": "owner", "rule": {"every_minutes": 60}})
    assert not engine.recurring({"created_by": "system", "rule": {"every_minutes": 5}})
    assert not engine.recurring({"created_by": "owner", "rule": {"once": True}})


@pytest.mark.parametrize("said,rule", [
    ("every monday, wednesday and friday at 9 pm", {"days": ["mon", "wed", "fri"], "time": "21:00"}),
    ("every monday wednesday and friday at 9 pm", {"days": ["mon", "wed", "fri"], "time": "21:00"}),
    ("every weeknight at 8", {"days": WD, "time": "20:00"}),
    ("on weeknights at 10", {"days": WD, "time": "22:00"}),
    ("every night at 11 o'clock", {"days": ALL, "time": "23:00"}),
    ("every day at half past 7 am", {"days": ALL, "time": "07:30"}),
    ("every tuesday at quarter to 8 pm", {"days": ["tue"], "time": "19:45"}),
])
def test_day_lists_weeknights_and_clock_words(said, rule):
    assert when.recurring(said) == rule


def test_a_command_with_a_day_list_splits_cleanly():
    rule, what, _ = when.split_recurring("lock the front door every monday, wednesday and friday at 9 pm")
    assert rule == {"days": ["mon", "wed", "fri"], "time": "21:00"} and what == "lock the front door"
    rule, what, _ = when.split_recurring("turn off the lights at 11 pm every weeknight")
    assert rule == {"days": WD, "time": "23:00"} and what == "turn off the lights"


@pytest.mark.parametrize("said,expect", [
    ("at quarter to 7 pm", "2026-10-03T18:45"), ("at quarter to 3", "2026-10-04T14:45"),
    ("at quarter to 12 pm", "2026-10-04T11:45"), ("at half past 8 pm", "2026-10-03T20:30"),
    ("at 9 o'clock tonight", "2026-10-03T21:00"),
])
def test_clock_words(said, expect):
    assert when.parse(said, NOW, "America/Chicago").strftime("%Y-%m-%dT%H:%M") == expect


def test_a_bare_quarter_to_seven_is_never_guessed():
    with pytest.raises(when.Unclear):
        when.parse("at quarter to 7", NOW, "America/Chicago")
    assert when.recurring("every day at quarter to 7") is None
