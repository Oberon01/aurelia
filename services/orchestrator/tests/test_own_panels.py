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


def test_a_late_confirmation_card_goes_to_the_display_that_asked_and_never_to_a_tv():
    from app.events import for_display
    ev = {"type": "confirm.ask", "device": 7, "data": {}}
    assert for_display(ev, {"id": 7, "kind": "browser"}) and not for_display(ev, {"id": 8, "kind": "browser"})
    loose = {"type": "confirm.ask", "device": None}
    assert not for_display(loose, {"id": 9, "kind": "screen"})
    assert not for_display(loose, {"id": 3, "kind": "browser", "role": "presentation"})
