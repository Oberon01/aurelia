"""Pulsar's rules that need no database: what counts as repeating, whose OK is for which run, and a system user
that may only look."""
import os
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")
os.environ.setdefault("REDIS_URL", "redis://localhost")

import pytest

from app import engine, when
from app.governance import GovernanceStore


@pytest.mark.parametrize("said,repeats", [
    ("lock the door every night at 11", True), ("shut down the den pc every weeknight at 1", True),
    ("lock the door nightly", True), ("daily at 7 am check the weather", True), ("on weekdays at 7", True),
    ("mondays at 6 pm", True), ("on the 1st of every month at 9", True), ("every 2 hours peek the yard", True),
    # Not a time: these stay commands.
    ("turn off every light", False), ("lock every door", False), ("play my daily mix", False),
    ("play discover weekly", False), ("take my daily vitamin", False), ("play the weekend playlist", False),
    ("show everything", False), ("each of them", False),
])
def test_what_makes_something_repeat(said, repeats):
    assert when.mentions_repeat(said) is repeats


def test_a_reminder_that_starts_with_how_often_repeats():
    assert when.repeat_first("every day to take my vitamins")
    assert not when.repeat_first("take my daily vitamins")


def _job(planned, rule):
    return {"created_by": "owner", "rule": rule, "planned_at": planned, "due_at": planned}


def test_his_ok_is_for_the_run_it_was_given_for():
    at = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)
    nxt = datetime(2026, 10, 6, 4, 0, tzinfo=timezone.utc)
    auto = {"days": ["mon"], "time": "23:00"}
    assert engine._approved(_job(at, auto), {"approved": at.timestamp()})            # this run (and its retry)
    assert not engine._approved(_job(nxt, auto), {"approved": at.timestamp()})       # not the next one
    assert not engine._approved(_job(at, auto), {})
    assert not engine._approved(_job(at, auto), {"approved": True})                  # an old-style OK: never an automation's
    assert engine._approved(_job(at, {"once": True}), {"approved": True})
    assert not engine._approved(_job(at, auto), {"approved": "yes"})


CHARTER = """
version: 1
enforced:
  default_tier: change
  tiers: {reference: {}, read: {}, act: {}, change: {requires_confirmation: true}, never: {blocked: true}}
users:
  owner: {display_name: Kentrell, role: owner}
  pulsar:
    display_name: Pulsar
    role: system
    allow:
      orbit: [status]
"""


def test_a_look_only_user_may_run_only_what_it_is_allowed(tmp_path):
    """Its own small charter, so this runs in the image's test stage too (the repo's config isn't copied there)."""
    (tmp_path / "functions").mkdir()
    (tmp_path / "charter.yaml").write_text(CHARTER)
    for fn_, tools in (("orbit", "status: {tier: reference}\n  lock: {tier: act}\n  unlock: {tier: change}"),
                       ("constellation", "power: {tier: change}"), ("pulsar", "save: {tier: change}")):
        (tmp_path / "functions" / f"{fn_}.yaml").write_text(f"function: {fn_}\nenabled: true\ntools:\n  {tools}\n")
    g = GovernanceStore(str(tmp_path))
    g.load_all()
    assert g.decide("orbit", "status", "pulsar", False).allowed
    for fn_, tool in (("orbit", "lock"), ("orbit", "unlock"), ("orbit", "something_new"), ("constellation", "power"),
                      ("pulsar", "save")):
        d = g.decide(fn_, tool, "pulsar", True)
        assert not d.allowed and d.tier == "never", (fn_, tool)
    assert g.decide("orbit", "status", "owner", False).allowed                       # his own rules are untouched
    assert g.decide("orbit", "lock", "owner", False).allowed
