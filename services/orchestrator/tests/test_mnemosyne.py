"""Mnemosyne without a database: cutting conversations, transcripts, reading the model's summary."""
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import mnemosyne

TZ = ZoneInfo("America/Chicago")
T0 = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)          # 9:00 PM Central


def req(minutes, text, reply="ok", tasks=(), screen=None, route="llm"):
    return {"id": uuid.uuid4(), "created_at": T0 + timedelta(minutes=minutes), "text": text, "reply": reply,
            "route": route, "task_ids": list(tasks), "screen": screen}


def test_conversations_are_cut_at_quiet_gaps():
    rows = [req(0, "a"), req(10, "b"), req(45, "c"), req(200, "d"), req(229, "e")]
    segs = mnemosyne.split_segments(rows, timedelta(minutes=30))
    assert [[r["text"] for r in s] for s in segs] == [["a", "b"], ["c"], ["d", "e"]]


def test_transcript_shows_times_focus_and_tools():
    t = uuid.uuid4()
    screen = {"panels": [{"n": 2, "task_id": "x", "tool": "nebula.search", "title": "Search: frieren"}], "focus": 2}
    text = mnemosyne.transcript([req(0, "download the third one", "Downloading it.", [t], screen)],
                                {str(t): ("nebula.download", "completed")}, TZ, 10_000)
    assert text.splitlines() == [
        "[9:00 PM] Kentrell (about panel 2: Search: frieren): download the third one",
        "  ran: nebula.download (completed)",
        "[9:00 PM] Aurelia: Downloading it."]


def test_long_transcripts_keep_the_end():
    rows = [req(i, f"question {i} " + "x" * 200) for i in range(100)]
    text = mnemosyne.transcript(rows, {}, TZ, 3000)
    assert text.startswith("…(earlier part cut)") and "question 99" in text and len(text) < 3100


def test_the_models_summary_is_read_carefully():
    ok = mnemosyne.parse_summary('Sure! {"summary": "Looked up Frieren.", "decisions": ["watch it Friday", ""],'
                                 ' "open": ["find the OST"], "topics": ["Anime", "Frieren"]} Hope that helps')
    assert ok == {"summary": "Looked up Frieren.", "decisions": ["watch it Friday"], "open": ["find the OST"],
                  "topics": ["anime", "frieren"], "about_kentrell": []}
    assert mnemosyne.parse_summary("no json here") is None
    assert mnemosyne.parse_summary('{"summary": ""}') is None
    assert mnemosyne.parse_summary('{"decisions": ["x"]}') is None
    assert mnemosyne.parse_summary('{"summary": "x", "decisions": "not a list"}')["decisions"] == []


def test_plain_record_lists_what_was_asked():
    rows = [req(i, f"q{i}") for i in range(10)]
    assert mnemosyne.plain_summary(rows)["summary"] == "Asked: q0; q1; q2; q3; q4; q5; q6; q7 (+2 more)"


def test_when_labels():
    a = T0
    assert mnemosyne._when(a, a + timedelta(minutes=1), TZ) == "Sun Sep 27, 9:00 PM"
    assert mnemosyne._when(a, a + timedelta(minutes=75), TZ) == "Sun Sep 27, 9:00 PM – 10:15 PM"
