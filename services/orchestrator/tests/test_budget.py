"""The budget's month: midnight on the 1st in his time zone, through daylight saving."""
from datetime import datetime, timezone

from app import budget


def test_the_month_starts_at_his_midnight_not_the_servers():
    # 11:30 PM Central on Oct 31 is already November in UTC, but it's still his October
    start, nxt = budget.month_bounds(datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc), "America/Chicago")
    assert (start.month, start.day, start.hour) == (10, 1, 0) and start.utcoffset().total_seconds() == -5 * 3600
    assert (nxt.month, nxt.day, nxt.hour) == (11, 1, 0)
    assert nxt.astimezone(timezone.utc) == datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc)


def test_the_next_month_keeps_its_own_offset_across_daylight_saving():
    start, nxt = budget.month_bounds(datetime(2026, 11, 15, 12, tzinfo=timezone.utc), "America/Chicago")
    assert start.astimezone(timezone.utc) == datetime(2026, 11, 1, 5, tzinfo=timezone.utc)      # CDT
    assert nxt.astimezone(timezone.utc) == datetime(2026, 12, 1, 6, tzinfo=timezone.utc)        # CST
    _, dec = budget.month_bounds(datetime(2026, 12, 20, tzinfo=timezone.utc), "America/Chicago")
    assert (dec.year, dec.month, dec.day) == (2027, 1, 1)


def test_amounts_and_times_read_plainly():
    assert budget._tokens(4_000_000) == "4.00M" and budget._tokens(52_340) == "52K" and budget._tokens(12) == "12"
    from datetime import timedelta
    assert budget._in(timedelta(minutes=20)) == "in under an hour"
    assert budget._in(timedelta(hours=5)) == "in 5 hours"
    assert budget._in(timedelta(days=28, hours=3)) == "in 28 days"
