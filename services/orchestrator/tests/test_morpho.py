"""Morpho's checks that need no database: phrases as fast paths, slots, which skills need the model, the
trust allow-list and its never-list, and drafts validated like plans."""
import pytest

from app import admiral, morpho

CAT = {
    "nebula.search": {"description": "", "schema": {"type": "object", "properties": {"query": {"type": "string"}}}, "tier": "reference"},
    "kepler.research": {"description": "", "schema": {"type": "object", "properties": {"question": {"type": "string"}}}, "tier": "read"},
    "orbit.light": {"description": "", "schema": {"type": "object", "properties": {"name": {"type": "string"}, "on": {"type": "boolean"}}}, "tier": "act"},
}


def test_a_phrase_matches_his_words_and_fills_the_slots():
    rx = morpho.phrase_regex("Find the best {product} under {budget}")
    m = rx.fullmatch("find the best gaming monitor under $400")
    assert m.groupdict() == {"product": "gaming monitor", "budget": "$400"}
    assert morpho.phrase_regex("{a} and {a}") is None                     # a slot used twice can't be read
    assert morpho.phrase_regex("movie night").fullmatch("Movie   Night")


def test_slots_fill_every_step_and_come_back_as_slots():
    steps = [{"tool": "nebula.search", "args": {"query": "best {product} under {budget} 2026"}}]
    filled = morpho.fill(steps, {"product": "monitor", "budget": "$400"})
    assert filled[0]["args"]["query"] == "best monitor under $400 2026"
    assert morpho.placeholders(steps) == {"product", "budget"}
    assert morpho._generalize(filled[0]["args"], {"product": "monitor", "budget": "$400"}) == steps[0]["args"]


def test_a_skill_with_no_fill_choice_or_report_needs_no_model():
    on = {"steps": [{"tool": "orbit.light", "args": {}, "fill": False}], "report": ""}
    assert morpho.model_free(on)
    assert not morpho.model_free({**on, "report": "compare them"})
    assert not morpho.model_free({**on, "steps": [{"tool": morpho.CHOOSE, "args": {}, "fill": True}]})


def test_a_draft_is_checked_like_a_plan():
    raw = {"name": "Best thing under a budget", "slots": [{"name": "Product", "example": "laptop"}, {"name": "budget", "example": "$1000"},
                                                          {"name": "unused", "example": "x"}],
           "steps": [{"tool": "nebula.search", "args": {"query": "best {product} under {budget}", "bogus": 1}},
                     {"tool": "kepler.research", "args": {"question": "compare {product}s"}, "depends": [1], "fill": True}],
           "report": "which is best", "phrases": ["find the best {product} under {budget}", "best {product}", "{product} {budget} {nope}"]}
    d = morpho._checked(raw, CAT, 2)
    assert [s["name"] for s in d["slots"]] == ["product", "budget"]            # unused slot dropped, names cleaned
    assert d["steps"][0]["args"] == {"query": "best {product} under {budget}"}
    assert d["phrases"] == ["find the best {product} under {budget}"]           # every phrase uses every slot
    with pytest.raises(ValueError, match="doesn't define"):
        morpho._checked({**raw, "slots": []}, CAT, 2)
    with pytest.raises(ValueError, match="don't match"):
        morpho._checked(raw, CAT, 3)
    with pytest.raises(ValueError):
        morpho._checked({**raw, "steps": [{"tool": "orbit.unlock"}]}, CAT)


def test_a_choice_step_offers_real_tools_only():
    s = admiral.validate({"steps": [{"tool": admiral.CHOOSE, "title": "Read", "args": {"options": [
        {"tool": "nebula.search", "args": {"query": "x"}}, {"tool": "kepler.research", "args": {"question": "y"}}]}}]}, CAT)
    assert s[0]["tool"] == admiral.CHOOSE and s[0]["fill"] and len(s[0]["args"]["options"]) == 2
    with pytest.raises(ValueError):
        admiral.validate({"steps": [{"tool": admiral.CHOOSE, "args": {"options": [{"tool": "nebula.search"}]}}]}, CAT)


class _Pol:
    rules = {"trust": {"eligible": [{"tool": "constellation.restart", "args": {"action": "restart"}}]}}


class _App:
    class state:
        class governance:
            functions = {"morpho": _Pol}


def test_with_no_allow_list_in_his_governance_nothing_can_earn_trust():
    class Bare:
        class state:
            class governance:
                functions = {}
    assert not morpho.eligible(Bare(), "constellation.restart", {"action": "restart"})


def test_trust_is_only_for_his_allow_list_and_never_for_the_never_list():
    app = _App()
    assert morpho.eligible(app, "constellation.restart", {"action": "restart", "target": "desk"})
    assert not morpho.eligible(app, "constellation.restart", {"action": "shutdown", "target": "desk"})
    assert not morpho.eligible(app, "orbit.lock", {"name": "front door"})              # not on his list
    for tool in ("orbit.unlock", "constellation.install_package", "constellation.kill_process", "constellation.sign_in",
                 "constellation.submit", "nebula.purchase", "kepler.forget", "chrysalis.rollback", "admiral.schedule",
                 "constellation.send", "quasar.speaker"):
        assert morpho.never(tool, {}), tool


def test_a_new_version_shows_as_a_diff():
    old = {"steps": [{"tool": "nebula.search", "args": {"query": "a"}}, {"tool": "kepler.research", "args": {}}]}
    new = {"steps": [{"tool": "nebula.search", "args": {"query": "a"}},
                     {"tool": morpho.CHOOSE, "args": {"options": [{"tool": "kepler.research"}, {"tool": "nebula.read"}]}}]}
    assert morpho.diff(old, new) == ["Step 2: kepler.research {} → choose: kepler.research or nebula.read"]


def test_a_phrase_must_have_words_of_its_own():
    assert morpho.phrase_ok("find the best {product} under {budget}")
    assert morpho.phrase_ok("movie night")
    for bad in ("{product}", "{a} {b}", "best {product}", "{product} under {budget} please", "find {a}{b} now", 3):
        assert not morpho.phrase_ok(bad), bad
    assert morpho.norm_phrase("Find the  best {Product}") == "Find the best {product}"


def test_slot_values_go_back_only_as_whole_values():
    assert morpho._generalize({"q": "under $1400 for 4 people", "n": 4}, {"budget": "4"}) == \
        {"q": "under $1400 for {budget} people", "n": "{budget}"}
