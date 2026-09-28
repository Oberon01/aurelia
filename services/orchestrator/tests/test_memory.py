from datetime import datetime, timedelta, timezone

from app.memory import age_label, args_key, snapshot_view, source_text, summarize


def test_same_task_despite_spacing_case_and_empty_args():
    assert args_key({"name": " Front  Door "}) == args_key({"name": "front door"})
    assert args_key({"name": "front door", "extra": None}) == args_key({"name": "front door"})
    assert args_key({}) == args_key(None)
    assert args_key({"name": "front door"}) != args_key({"name": "back door"})


def test_summary_keeps_shape_top_items_and_counts():
    data = {"devices": [{"name": f"d{i}", "on": True} for i in range(9)], "note": "x" * 400, "count": 9}
    s = summarize(data)
    assert [d["name"] for d in s["devices"]] == ["d0", "d1", "d2", "d3", "d4"]
    assert s["_more"] == {"devices": 4} and s["count"] == 9
    assert len(s["note"]) == 298 and s["note"].endswith("…")
    assert summarize({"devices": [1, 2]}) == {"devices": [1, 2]}                  # short lists untouched


def test_source_text_reads_like_the_panel():
    text = source_text("orbit.status", {}, {"devices": [
        {"name": "Front Door", "kind": "lock", "locked": True}, {"name": "Thermostat", "current_temp": 71}]})
    assert "orbit status" in text and "name Front Door" in text and "current temp 71" in text
    assert len(source_text("x.y", {}, {"a": "word " * 2000})) <= 2000


def test_restored_panel_carries_age_staleness_and_tier():
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    row = {"task_id": "0b6f1f64-0000-0000-0000-000000000000", "captured_at": old, "tier": "hot",
           "pinned": False, "partial": False, "panel": '{"devices": []}', "summary": None}
    v = snapshot_view(row, freshness_seconds=30)
    assert v["age"] == "10 min ago" and v["stale"] is True and v["tier"] == "hot" and v["data"] == {"devices": []}
    assert snapshot_view({**row, "pinned": True, "panel": None, "summary": {"a": 1}}, None)["tier"] == "pinned"
    assert snapshot_view(row, None)["stale"] is False                              # no freshness declared
    assert [age_label(s) for s in (5, 120, 7200, 172800)] == ["just now", "2 min ago", "2 h ago", "2 d ago"]
