"""Reading a time from his words, by code (not the model), in his time zone.

Understood: "in 20 minutes", "in an hour", "in half an hour", "in 2 days"; "at 5", "at 5:30 pm", "at noon",
"at midnight", "17:45"; "today/tonight/tomorrow (morning/afternoon/evening/night) (at …)"; "Friday at 9",
"this Friday", "next Friday", "on Oct 5 at 3pm". Anything else is refused with a plain question, never
guessed. Every result is shown back to him as an exact time before it counts.

A bare hour: 1-6 means PM ("at 5" is 5 PM), 7-11 means the next time it comes round, 12 is noon; "tonight",
"evening" and "night" mean PM, "morning" means AM. A time today that has already passed means tomorrow.
"Next Friday" is the coming Friday, never today. Daylight saving: a time the clock skips (2:30 AM in March)
becomes the first minute after the jump; a time that happens twice (1:30 AM in November) is the first one.
"""
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

UNITS = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 604800}
WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
         "forty-five": 45, "fifty": 50, "sixty": 60, "ninety": 90, "a couple of": 2, "a couple": 2, "a few": 3}
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
PARTS = {"morning": (9, 0), "afternoon": (14, 0), "evening": (18, 0), "night": (21, 0)}
HORIZON = timedelta(days=366)


class Unclear(ValueError):
    pass


def localize(naive: datetime, tz: ZoneInfo) -> datetime:
    """A wall-clock time in his zone: skipped times move to just after the jump; repeated ones are the first."""
    t = naive
    for _ in range(181):
        aware = t.replace(tzinfo=tz, fold=0)
        if aware.astimezone(ZoneInfo("UTC")).astimezone(tz).replace(tzinfo=None) == t:
            return aware
        t += timedelta(minutes=1)
    return naive.replace(tzinfo=tz)


def _clean(text: str) -> str:
    t = " ".join(str(text or "").lower().replace(",", " ").split()).strip(" .!?")
    t = re.sub(r"\b([ap])\.\s?m\.?", r"\1m", t)            # p.m. -> pm
    t = re.sub(r"(\d)\s+([ap]m)\b", r"\1\2", t)             # 5 pm -> 5pm
    return t


def _clock(s: str) -> tuple[int, int, str | None] | None:
    """'5', '5:30', '5pm', '17:45', 'noon', 'midnight' -> (hour, minute, 'am'|'pm'|'24'|None)."""
    if s == "noon":
        return 12, 0, "pm"
    if s == "midnight":
        return 0, 0, "24"
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", s)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if mi > 59 or h > 23 or (ap and not 1 <= h <= 12):
        return None
    if ap is None and (h == 0 or h > 12 or (m.group(2) and len(m.group(1)) == 2 and h >= 13)):
        ap = "24"
    return h, mi, ap


def _hour24(h: int, mi: int, ap: str | None, part: str | None) -> list[tuple[int, int, int]]:
    """The wall-clock candidates for a clock reading, best first, as (hour, minute, days after the day said).
    At night, 12 and 1-4 are the small hours after midnight ("tonight at 1" is 1 AM)."""
    night = part in ("night", "tonight")
    if ap == "24":
        return [(h, mi, 1 if night and h < 5 else 0)]      # "tonight at midnight" is the midnight ending tonight
    if ap == "am":
        return [(0 if h == 12 else h, mi, 1 if night else 0)]
    if ap == "pm":
        return [(12 if h == 12 else h + 12, mi, 0)]
    if night and (h == 12 or 1 <= h <= 4):
        return [(0 if h == 12 else h, mi, 1)]
    if part in ("evening", "night", "tonight", "afternoon"):
        return [(12 if h == 12 else h + 12, mi, 0)]
    if part == "morning":
        return [(0 if h == 12 else h, mi, 0)]
    if h == 12:
        return [(12, mi, 0)]
    if 1 <= h <= 6:
        return [(h + 12, mi, 0)]
    return [(h, mi, 0), (h + 12, mi, 0)]                  # 7-11: whichever comes next


def _relative(t: str) -> timedelta | None:
    m = re.fullmatch(r"in\s+(half\s+an?|an?\s+hour\s+and\s+a\s+half|a\s+couple(?:\s+of)?|a\s+few|[\d.]+|[a-z-]+)\s*"
                     r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)?", t)
    if not m:
        return None
    n_s, unit = " ".join(m.group(1).split()), (m.group(2) or "")
    if n_s.startswith("half"):
        n, unit = 0.5, unit or "hour"
    elif "and a half" in n_s:
        n, unit = 1.5, "hour"
    elif re.fullmatch(r"[\d.]+", n_s):
        try:
            n = float(n_s)
        except ValueError:
            return None
    elif n_s in WORDS:
        n = WORDS[n_s]
    else:
        return None
    if not unit:
        return None
    base = unit.rstrip("s") if unit not in ("secs",) else "sec"
    secs = UNITS.get(base) or UNITS.get(base[:3])
    if not secs or n <= 0:
        return None
    return timedelta(seconds=n * secs)


def _day(t: str, today: date) -> tuple[date | None, str | None, str]:
    """Pull a day off the front or back of t: (the date, the part of day it implies, what's left)."""
    pats = [
        (r"today", lambda m: (today, None)),
        (r"tonight", lambda m: (today, "tonight")),
        (r"this\s+(morning|afternoon|evening)", lambda m: (today, m.group(1))),
        (r"tomorrow(?:\s+(morning|afternoon|evening|night))?", lambda m: (today + timedelta(days=1), m.group(1))),
        (r"(?:on\s+|this\s+|next\s+)?(" + "|".join(DAYS) + r")(?:\s+(morning|afternoon|evening|night))?",
         lambda m: (_weekday(today, DAYS.index(m.group(1)), m.group(0).startswith("next")), m.group(2))),
        (r"(?:on\s+)?(" + "|".join(x[:3] + "(?:" + x[3:] + ")?" for x in MONTHS) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?",
         lambda m: (_month_day(today, m.group(1), int(m.group(2))), None)),
    ]
    for p, f in pats:
        for rx in (rf"^(?P<d>{p})\b\s*(?P<rest>.*)$", rf"^(?P<rest>.*?)\s*\b(?P<d>{p})$"):
            m = re.match(rx, t)
            if m:
                d, part = f(re.fullmatch(p, m.group("d")))
                if d is None:
                    raise Unclear("I can't tell which date that is.")
                return d, part, m.group("rest").strip()
    return None, None, t


def _weekday(today: date, wd: int, nxt: bool) -> date:
    ahead = (wd - today.weekday()) % 7
    if ahead == 0 and nxt:
        ahead = 7
    return today + timedelta(days=ahead)


def _month_day(today: date, mon: str, day: int) -> date | None:
    month = next(i for i, x in enumerate(MONTHS, 1) if x.startswith(mon[:3]))
    for year in (today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if d >= today:
            return d
    return None


def parse(text: str, now: datetime, tz: str = "America/Chicago") -> datetime:
    """His words -> an exact time (aware, in his zone). Raises Unclear with a question when it can't tell."""
    z = ZoneInfo(tz)
    now = now.astimezone(z)
    t = _clean(text)
    if not t:
        raise Unclear("When should I do it?")
    rel = _relative(t)
    if rel is not None:
        if rel > HORIZON:
            raise Unclear("That's more than a year away; give me a date.")
        return (now.astimezone(ZoneInfo("UTC")) + rel).astimezone(z)    # real elapsed time, across a clock change
    day, part, rest = _day(t, now.date())
    rest = re.sub(r"^(?:at|@)\s+", "", rest).strip()
    if not rest:
        if part in PARTS or part == "tonight":
            h, mi = PARTS["night" if part == "tonight" else part]
            cands = [(h, mi, 0)]
        elif day is not None:
            raise Unclear("At what time?")
        else:
            raise Unclear(f"I can't tell when \"{text}\" is. Try \"in 20 minutes\", \"at 5:30 pm\" or \"tomorrow at 8\".")
    else:
        c = _clock(rest)
        if c is None:
            raise Unclear(f"I can't tell when \"{text}\" is. Try \"in 20 minutes\", \"at 5:30 pm\" or \"tomorrow at 8\".")
        cands = _hour24(*c, part)
    if not re.search(r"\b(?:at|@)\b|\d(?:am|pm)|:\d\d|noon|midnight", t) and day is None and rest:
        raise Unclear(f"Did you mean at {rest}? Say \"at {rest}\".")   # a bare number isn't a time
    explicit_day = day is not None
    base = day or now.date()
    options = []
    for h, mi, plus in cands:
        d = base + timedelta(days=plus)
        at = localize(datetime.combine(d, time(h, mi)), z)
        if not explicit_day and at <= now:
            at = localize(datetime.combine(d + timedelta(days=1), time(h, mi)), z)
        options.append(at)
    future = [o for o in options if o > now]
    if not future:
        raise Unclear("That time has already passed.")
    return min(future) if not explicit_day or len(future) == 1 else future[0]


def split(say: str, now: datetime, tz: str = "America/Chicago") -> tuple[datetime, str, str]:
    """'at 11 pm lock the front door' / 'lock the front door in 20 minutes' -> (when, what, the time words).
    The longest time phrase at the start or the end wins; what's left is the command."""
    words = " ".join(str(say or "").replace(",", " ").split()).strip(" .!?").split()   # his case kept for what's left
    found = []
    for k in range(len(words) - 1, 0, -1):
        for when_w, what_w in ((words[:k], words[k:]), (words[-k:], words[:-k])):
            try:
                at = parse(" ".join(when_w), now, tz)
            except Unclear:
                continue
            found.append((k, at, " ".join(what_w), " ".join(when_w)))
        if found:
            break
    if not found:
        raise Unclear("I couldn't find when in that. Say it like \"at 11 pm, lock the front door\" or "
                      "\"turn off the lights in 20 minutes\".")
    _, at, what, when_w = found[0]
    return at, what, when_w


def say(at: datetime, now: datetime | None = None) -> str:
    """'Sat, Oct 3, 11:00 PM' (with 'today'/'tomorrow' when it's close)."""
    clock = at.strftime("%I:%M %p").lstrip("0")
    if now is not None:
        n = now.astimezone(at.tzinfo)
        if at.date() == n.date():
            return f"today at {clock}"
        if at.date() == n.date() + timedelta(days=1):
            return f"tomorrow at {clock}"
    return f"{at.strftime('%a, %b')} {at.day}, {clock}"
