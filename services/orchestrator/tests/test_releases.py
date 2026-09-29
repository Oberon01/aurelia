from app.releases import ReleaseState, events_for


def test_running_release_opens_then_reports_progress():
    s = ReleaseState(release_id=7, sha="a1b2c3d4", message="Display: queue", current=2, total=7, label="Building orchestrator")
    evs = events_for(s)
    assert [e.type for e in evs] == ["panel.open", "panel.progress"]
    assert evs[0].task_id == "release-7" and evs[0].tool == "chrysalis.release"
    assert evs[0].data["subtitle"] == "Display: queue" and evs[0].data["cancellable"] is False
    assert evs[1].data == {"current": 2, "total": 7, "unit": "steps", "label": "Building orchestrator"}
    assert [e.type for e in events_for(s, include_open=False)] == ["panel.progress"]


def test_outcomes_fade_only_when_live():
    live = events_for(ReleaseState(release_id=7, sha="a1b2c3d4", outcome="live", detail="ok"), include_open=False)
    assert [e.type for e in live] == ["panel.result", "panel.close"]
    assert live[0].data["title"] == "Release live" and live[0].data["release"]["outcome"] == "live"
    assert live[1].data["close_after"] == 20
    back = events_for(ReleaseState(release_id=8, sha="b", outcome="rolled_back", detail="smoke test failed"), False)
    assert back[0].data["title"] == "Release rolled back" and back[1].data["close_after"] is None
