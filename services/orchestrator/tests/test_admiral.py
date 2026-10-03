"""Admiral's checks that need no database: the model's plan is validated before anything runs, and a
step's own result decides whether it counts as done."""
import pytest

from app import admiral, auth

CAT = {
    "nebula.search": {"description": "search", "schema": {"type": "object", "properties": {"q": {"type": "string"}},
                                                          "required": ["q"]}, "tier": "reference"},
    "nebula.fetch": {"description": "fetch", "schema": {"type": "object", "properties": {"url": {"type": "string"}}},
                     "tier": "reference"},
    "constellation.send": {"description": "send", "schema": {"type": "object", "properties": {}}, "tier": "act"},
}


def test_a_good_plan_is_kept_with_only_what_the_tools_take():
    steps = admiral.validate({"steps": [
        {"title": "Search", "tool": "nebula.search", "args": {"q": "laptops", "bogus": 1}},
        {"title": "Read", "tool": "nebula.fetch", "args": {}, "depends": [1, "1", 5, 2, "x"], "fill": True},
        {"title": "Independent", "tool": "nebula.search", "args": {"q": "x"}, "fill": True},
    ]}, CAT)
    assert steps[0] == {"n": 1, "title": "Search", "tool": "nebula.search", "args": {"q": "laptops"}, "depends": [],
                        "fill": False}
    assert steps[1]["depends"] == [1] and steps[1]["fill"] is True          # only earlier steps; never itself or later
    assert steps[2]["fill"] is False                                         # nothing to fill from


@pytest.mark.parametrize("raw,why", [
    ({"steps": []}, "no steps"),
    ({}, "no steps"),
    ([1, 2], "no steps"),
    ({"steps": [{"tool": "nebula.delete_everything"}]}, "don't have"),
    ({"steps": [{"tool": "admiral.plan", "args": {"goal": "x"}}]}, "don't have"),     # never a plan inside a plan
    ({"steps": [{"tool": "nebula.search", "args": {"q": "x"}}] * (admiral.MAX_STEPS + 1)}, "at most"),
])
def test_a_bad_plan_never_runs(raw, why):
    with pytest.raises(ValueError, match=why):
        admiral.validate(raw, CAT)


def test_the_models_json_is_found_even_with_fences_or_a_sentence():
    assert admiral._json_in('```json\n{"steps": []}\n```') == {"steps": []}
    assert admiral._json_in('Here you go: {"a": {"b": 1}} hope that helps') == {"a": {"b": 1}}
    with pytest.raises(ValueError):
        admiral._json_in("no json here")


def test_only_a_result_that_confirms_itself_counts_as_done():
    assert admiral.unconfirmed({"headline": "Sent", "did": {"node": "TV"}}) is None
    assert admiral.unconfirmed({"did": {"what": "asked", "unconfirmed": True}}) == "asked"
    assert admiral.unconfirmed({"items": [{"ok": 1}, {"verified": False, "note": "page never loaded"}]}) == "page never loaded"
    assert admiral.unconfirmed({"sent": {"confirmed": False}}) == "it couldn't be confirmed"
    assert admiral.unconfirmed({"confirmed": True, "verified": True}) is None
    # Constellation's send: shown on the displays in "confirmed", or only kept for later
    assert admiral.unconfirmed({"sent": {"what": "panel", "confirmed": ["TV"], "later": [], "failed": []}}) is None
    assert admiral.unconfirmed({"sent": {"what": "panel", "confirmed": [], "later": ["TV"], "failed": []}}) == \
        "no display has shown it yet"
    # Constellation's open: what the computer couldn't see start
    assert admiral.unconfirmed({"opened": {"node": "Desk", "what": "vlc", "kind": "app", "unconfirmed": True}}) == "Desk couldn't see vlc start"


def test_steps_run_when_what_they_need_is_done_and_never_after_a_failure():
    def st(n, status, deps=()):
        return {"n": n, "status": status, "depends": list(deps)}
    steps = [st(1, "done"), st(2, "unconfirmed"), st(3, "pending", [1]), st(4, "pending", [2]), st(5, "pending", [4]),
             st(6, "pending", [1, 3])]
    assert [s["n"] for s in admiral.ready(steps)] == [3]
    assert [s["n"] for s in admiral.blocked(steps)] == [4, 5]                # and what depends on those, in turn


def test_the_panel_shows_exactly_what_a_waiting_step_would_run():
    plan = {"id": "p1", "goal": "g", "status": "waiting", "error": None, "report": None}
    v = admiral.view(plan, [{"n": 1, "title": "Send it", "tool": "constellation.send", "status": "waiting",
                             "note": "needs your OK", "args": {"target": "tv"}},
                            {"n": 2, "title": "Look", "tool": "nebula.search", "status": "pending", "note": None,
                             "args": {"q": "secret-ish"}}])
    assert v["headline"] == "Plan · waiting for you"
    assert v["plan"]["steps"][0]["args"] == {"target": "tv"} and "args" not in v["plan"]["steps"][1]


def test_a_tv_screen_cant_decide_a_plan_step():
    assert not auth.allowed("screen", "/admiral/plans/abc/steps/1/decide")
    assert auth.allowed("browser", "/admiral/plans/abc/steps/1/decide")
    assert not auth.allowed("node", "/admiral/plans/abc/steps/1/decide")
