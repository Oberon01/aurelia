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
