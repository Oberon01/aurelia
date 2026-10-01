"""Which display gets which event (each display keeps its own panels)."""
from app.events import for_display

DESK, PHONE, TV = {"id": 1, "kind": "browser"}, {"id": 2, "kind": "browser"}, {"id": 3, "kind": "screen"}


def ev(type_="panel.open", device=None):
    return {"type": type_, "task_id": "t", "tool": "x.y", "data": {}, "device": device}


def test_a_panel_goes_only_to_the_display_that_asked():
    for t in ("panel.open", "panel.progress", "panel.result", "panel.close"):
        assert for_display(ev(t, 2), PHONE) and not for_display(ev(t, 2), DESK) and not for_display(ev(t, 2), TV)
    assert for_display(ev(device=3), TV)                       # asked on the TV itself: it's the TV's


def test_unaddressed_panels_skip_node_screens():
    assert for_display(ev(), DESK) and for_display(ev(), PHONE) and not for_display(ev(), TV)
    assert for_display({"type": "panel.open", "task_id": "t", "tool": "x"}, DESK)       # older events had no device


def test_everything_else_goes_everywhere_and_inside_gets_all():
    assert all(for_display(ev("mailbox.changed"), d) for d in (DESK, PHONE, TV))
    assert for_display(ev(device=2), None) and for_display(ev(), None)
