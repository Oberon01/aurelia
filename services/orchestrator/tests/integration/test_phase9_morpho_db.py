"""Morpho end to end: a plan kept as a skill (his approval, his name and phrases), reused by the planner and by
its phrase, a skill that runs with the model down, versions learned from a change of course and from a plan
that differs in one step (a choice), skills proposed from tools he uses together, and earned trust: proposed
from his approvals, only for his allow-list, used without asking, taken back by a no or a failure.
Real Postgres and Redis; the model is scripted; the steps' functions are the stand-ins of the other tests."""
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from .conftest import DB, REDIS
from .test_phase9_admiral_db import Model, make_plan, planfn, sql, step, until  # noqa: F401 (fixtures)
from .test_phase9_engine_db import enginefn  # noqa: F401

pytestmark = pytest.mark.skipif(not (DB and REDIS), reason="needs TEST_DATABASE_URL and TEST_REDIS_URL")


class SkillModel(Model):
    def __init__(self):
        super().__init__()
        self.generalize, self.choices, self.down = {}, {}, False

    async def complete(self, route, system, messages, tools):
        from app import admiral, morpho
        from app.llm.base import Completion, ProviderError, Usage
        if self.down:
            raise ProviderError("the model is down (test)")
        text = messages[-1].text
        if system == morpho.PHRASE_SYSTEM:
            self.calls.append(("phrase", text))
            goal = re.search(r"His request: (.*?)\n", text).group(1)
            return Completion(text=json.dumps(self.generalize[goal]), tool_calls=[], usage=Usage(10, 5),
                              model="fake-plan", provider="fake")
        if system == morpho.GENERALIZE_SYSTEM:
            self.calls.append(("generalize", text))
            goal = re.search(r"His request: (.*?)\n", text).group(1)
            return Completion(text=json.dumps(self.generalize[goal]), tool_calls=[], usage=Usage(10, 5),
                              model="fake-plan", provider="fake")
        if system == admiral.CHOOSE_SYSTEM:
            self.calls.append(("choose", text))
            goal = re.search(r"Goal: (.*?)\n", text).group(1)
            return Completion(text=json.dumps({"option": self.choices.get(goal, 1), "why": "test"}), tool_calls=[],
                              usage=Usage(10, 5), model="fake-plan", provider="fake")
        return await super().complete(route, system, messages, tools)


@pytest.fixture
def model(stack, planfn, enginefn):
    from app.main import app
    planfn["register"]()
    enginefn["register"]()
    for fn in ("admiral", "morpho"):
        deadline = time.time() + 20
        while time.time() < deadline and not any(f["name"] == fn for f in httpx.get(f"{stack['orch']}/registry/functions").json()):
            time.sleep(0.2)
    original, fake = app.state.llm, SkillModel()
    app.state.llm = fake
    yield fake
    app.state.llm = original


def task(orch, tool, args, wait=20, **extra):
    tid = httpx.post(f"{orch}/tasks", json={"tool": tool, "args": args, **extra}).json()["task_id"]
    deadline = time.time() + wait
    while time.time() < deadline:
        t = httpx.get(f"{orch}/tasks/{tid}").json()
        if t["status"] != "running":
            return t, (json.loads(t["result"]) if isinstance(t["result"], str) else t["result"])
        time.sleep(0.1)
    raise AssertionError(f"{tool} never finished")


def card(kind, **where):
    rows = sql("SELECT * FROM mailbox_items WHERE kind=$1 AND status='waiting' ORDER BY id DESC", kind)
    for r in rows:
        p = json.loads(r["proposal"])
        if all(p.get(k) == v for k, v in where.items()):
            return dict(r), p
    raise AssertionError(f"no waiting {kind} card like {where}")


def _route(path, fn_):
    from app.main import app
    if not any(getattr(r, "path", "") == path for r in app.routes):
        app.add_api_route(path, fn_, methods=["POST"])


def plan_of_task(task_id):
    deadline = time.time() + 10
    while time.time() < deadline:
        rows = sql("SELECT id FROM plans WHERE task_id=$1", uuid.UUID(task_id))
        if rows:
            return str(rows[0]["id"])
        time.sleep(0.1)
    raise AssertionError("no plan for that task")


COMPUTER = {"name": "Best thing under a budget",
            "slots": [{"name": "product", "example": "computer"}, {"name": "budget", "example": "$1000"}],
            "steps": [step(1, "planfn.look", {"q": "best {product} under {budget}"}),
                      step(2, "planfn.look", {"q": "reviews of {product}"}, depends=[1])],
            "report": "which {product} is best",
            "phrases": ["find the best {product} under {budget}", "best {product} for {budget}"]}


def test_a_plan_that_worked_becomes_a_skill_he_approves_and_the_planner_reuses(stack, model, planfn):
    orch, goal = stack["orch"], "find the best computer within a $1000 budget"
    sql("UPDATE skills SET status='retired' WHERE status IN ('active','proposed')")
    model.plans[goal] = {"steps": [step(1, "planfn.look", {"q": "best computer under $1000"}),
                                   step(2, "planfn.look", {"q": "reviews of computer"}, depends=[1])]}
    _, res = make_plan(orch, goal)
    pid = res["plan"]["id"]
    p, _ = until(pid, {"done", "gaps", "failed"})
    assert p["status"] == "done"
    model.generalize[goal] = COMPUTER
    r = httpx.post(f"{orch}/morpho/keep/{pid}")
    assert r.status_code == 200 and "mailbox" in r.json()["done"]
    assert httpx.post(f"{orch}/morpho/keep/{pid}").status_code == 409          # once
    item, prop = card("skill")
    assert prop["preview"]["slots"][0]["name"] == "product" and prop["preview"]["phrases"][0] == "find the best {product} under {budget}"
    r = httpx.post(f"{orch}/mailbox/{item['id']}/decide", json={"action": "modify", "changes": {
        "name": "Best gear", "phrases": "find the best {product} under {budget}; get me a {product} under {budget}; nope {product}"}})
    assert r.status_code == 200 and "Kept: Best gear" in r.json()["done"]
    [sk] = sql("SELECT * FROM skills WHERE id=$1", prop["skill_id"])
    assert sk["status"] == "active" and json.loads(sk["phrases"]) == ["find the best {product} under {budget}",
                                                                      "get me a {product} under {budget}"]
    # the planner reaches for it first
    g2 = "find the best monitor under $400"
    model.picks[g2] = {"skill": "Best gear", "slots": {"product": "monitor", "budget": "$400"}}
    _, res = make_plan(orch, g2)
    assert "from your skill Best gear" in res["headline"]
    p2, steps2 = until(res["plan"]["id"], {"done", "gaps", "failed"})
    assert p2["status"] == "done" and p2["skill_id"] == sk["id"]
    assert [json.loads(s["args"])["q"] for s in steps2] == ["best monitor under $400", "reviews of monitor"]
    assert "Skills:\n- Best gear" in next(t for _, t in model.calls if t.startswith(f"His request: {g2}"))
    assert not any(t.startswith(f"His request: {g2}\n\nTools:") for _, t in model.calls)   # no planning from scratch
    deadline = time.time() + 5
    while sql("SELECT uses FROM skills WHERE id=$1", sk["id"])[0]["uses"] < 1 and time.time() < deadline:
        time.sleep(0.1)
    assert sql("SELECT uses FROM skills WHERE id=$1", sk["id"])[0]["uses"] == 1


def test_the_same_plan_again_teaches_the_skill_a_phrasing_instead_of_making_a_second_skill(stack, model, planfn):
    orch, goal = stack["orch"], "what's the top tablet within a $300 budget"
    model.plans[goal] = {"steps": [step(1, "planfn.look", {"q": "best tablet under $300"}),
                                   step(2, "planfn.look", {"q": "reviews of tablet"}, depends=[1])]}   # no skill picked
    _, res = make_plan(orch, goal)
    pid = res["plan"]["id"]
    assert until(pid, {"done", "gaps", "failed"})[0]["status"] == "done"
    n_active = len(sql("SELECT 1 FROM skills WHERE status='active'"))
    model.generalize[goal] = {"phrases": ["what's the top {product} within a {budget} budget", "{product}"]}   # (PHRASE reply)
    r = httpx.post(f"{orch}/morpho/keep/{pid}")
    assert r.status_code == 200 and "instead of a second skill" in r.json()["done"]
    item, prop = card("skill")
    [v] = sql("SELECT * FROM skills WHERE id=$1", prop["skill_id"])
    assert v["parent_id"] and json.loads(v["phrases"])[-1] == "what's the top {product} within a {budget} budget"
    httpx.post(f"{orch}/mailbox/{item['id']}/decide", json={"action": "approve"}).raise_for_status()
    assert len(sql("SELECT 1 FROM skills WHERE status='active'")) == n_active        # still one skill, a new version
    out = httpx.post(f"{orch}/ask", json={"text": "what's the top phone within a $500 budget"}, timeout=30).json()
    assert out["route"] == "fastpath" and out["reply"].startswith("Running your skill")


def test_his_phrase_runs_the_skill_like_a_fast_path(stack, model, planfn):
    orch = stack["orch"]
    out = httpx.post(f"{orch}/ask", json={"text": "get me a keyboard under $80"}, timeout=30).json()
    assert out["route"] == "fastpath" and out["reply"] == "Running your skill: Best gear."
    pid = plan_of_task(out["task_ids"][0])
    p, steps = until(pid, {"done", "gaps", "failed"})
    assert p["status"] == "done" and json.loads(p["slots"]) == {"product": "keyboard", "budget": "$80"}
    assert json.loads(steps[0]["args"])["q"] == "best keyboard under $80"
    # with a time in it, it's never the skill now (it's for later, or a routine: the model hears it)
    before = len(sql("SELECT 1 FROM task_runs WHERE tool='morpho.run'"))
    model.down = True                                  # (the test's model can't chat; what matters is no skill ran)
    try:
        for said in ("get me a keyboard under $80 tomorrow at 9", "get me a keyboard under $80 every morning"):
            httpx.post(f"{orch}/ask", json={"text": said}, timeout=30)
    finally:
        model.down = False
    assert len(sql("SELECT 1 FROM task_runs WHERE tool='morpho.run'")) == before


def test_a_skill_that_needs_no_model_runs_with_the_model_down(stack, model, planfn):
    orch = stack["orch"]
    sk = sql("INSERT INTO skills (name, status, slots, steps, report, phrases, approved_at) VALUES ('Movie night', 'active', "
             "'[]'::jsonb, $1::jsonb, '', $2::jsonb, now()) RETURNING id",
             json.dumps([step(1, "planfn.act", {"what": "dim the lights"}), step(2, "planfn.look", {"q": "jellyfin"}, depends=[1])]),
             json.dumps(["movie night", "it's movie night"]))[0]["id"]
    model.down = True
    before = len(model.calls)
    out = httpx.post(f"{orch}/ask", json={"text": "Aura, it's movie night"}, timeout=30).json()
    assert out["route"] == "fastpath"
    p, steps = until(plan_of_task(out["task_ids"][0]), {"done", "gaps", "failed"})
    report = json.loads(p["report"])
    assert p["status"] == "done" and report["headline"] == "Movie night: 2 of 2 steps done"
    assert "dim the lights" in report["summary"] or "1. " in report["summary"]
    assert len(model.calls) == before                                          # not one model call
    t, res = task(orch, "morpho.skills", {})
    assert any(i["name"] == "Movie night" and i["model_free"] for i in res["skills"]["items"])
    model.down = False


def test_a_run_that_changed_course_is_learned_as_a_new_version(stack, model, planfn):
    orch = stack["orch"]
    sql("UPDATE skills SET status='retired' WHERE name='Fix it'")
    sk = sql("INSERT INTO skills (name, status, slots, steps, report, phrases, approved_at) VALUES ('Fix it', 'active', "
             "'[{\"name\": \"thing\", \"example\": \"x\"}]'::jsonb, $1::jsonb, 'r', '[]'::jsonb, now()) RETURNING id",
             json.dumps([step(1, "planfn.broken"), step(2, "planfn.look", {"q": "check {thing}"}, depends=[1])]))[0]["id"]
    t, res = task(orch, "morpho.run", {"skill": "Fix it", "slots": {"thing": "the sink"}})
    assert t["status"] == "completed", t["error"]
    goal = "Fix it: thing the sink"
    model.replans[goal] = [{"why": "Broken was down; looking instead.",
                            "steps": [step(3, "planfn.look", {"q": "another way for the sink"}),
                                      step(4, "planfn.look", {"q": "check the sink"}, depends=[3])]}]
    p, _ = until(res["plan"]["id"], {"done", "gaps", "failed"}, wait=30)
    assert p["status"] == "done"
    deadline = time.time() + 5
    while not sql("SELECT 1 FROM skills WHERE parent_id=$1", sk) and time.time() < deadline:
        time.sleep(0.1)
    [v2] = sql("SELECT * FROM skills WHERE parent_id=$1", sk)
    assert v2["status"] == "proposed" and v2["version"] == 2 and v2["source"] == "variation"
    assert [s["args"].get("q") for s in json.loads(v2["steps"])] == ["another way for {thing}", "check {thing}"]
    item, prop = card("skill", skill_id=v2["id"])
    assert prop["diff"][0].startswith("Step 1: planfn.broken") and "Broken was down" in json.loads(item["evidence"])[-1]
    httpx.post(f"{orch}/mailbox/{item['id']}/decide", json={"action": "approve"}).raise_for_status()
    assert [r["status"] for r in sql("SELECT status FROM skills WHERE id = ANY($1::bigint[]) ORDER BY id", [sk, v2["id"]])] == \
        ["superseded", "active"]
    t, res = task(orch, "morpho.rollback", {"skill": "Fix it"})
    assert t["status"] == "completed" and res["headline"] == "Fix it: back to version 1"
    assert sql("SELECT status FROM skills WHERE id=$1", sk)[0]["status"] == "active"


def test_a_plan_that_differs_in_one_step_makes_that_step_a_choice(stack, model, planfn):
    orch = stack["orch"]
    sql("UPDATE skills SET status='retired' WHERE status='active'")
    sk = sql("INSERT INTO skills (name, status, slots, steps, report, phrases, approved_at) VALUES ('Two step', 'active', "
             "'[]'::jsonb, $1::jsonb, 'r', '[]'::jsonb, now()) RETURNING id",
             json.dumps([step(1, "planfn.look", {"q": "first"}), step(2, "planfn.act", {"what": "second"}, depends=[1])]))[0]["id"]
    goal = "do the two things differently"
    model.plans[goal] = {"steps": [step(1, "planfn.look", {"q": "first"}),
                                   step(2, "planfn.flaky", {"key": "choice-test"}, depends=[1])]}
    _, res = make_plan(orch, goal)
    p, _ = until(res["plan"]["id"], {"done", "gaps", "failed"}, wait=30)
    assert p["status"] == "done"
    deadline = time.time() + 5
    while not sql("SELECT 1 FROM skills WHERE parent_id=$1", sk) and time.time() < deadline:
        time.sleep(0.1)
    [v2] = sql("SELECT * FROM skills WHERE parent_id=$1", sk)
    s2 = json.loads(v2["steps"])[1]
    assert s2["tool"] == "admiral.choose" and [o["tool"] for o in s2["args"]["options"]] == ["planfn.act", "planfn.flaky"]
    item, _ = card("skill", skill_id=v2["id"])
    httpx.post(f"{orch}/mailbox/{item['id']}/decide", json={"action": "approve"}).raise_for_status()
    model.choices["Two step"] = 2                                            # this time, the second option
    t, res = task(orch, "morpho.run", {"skill": "Two step"})
    p, steps = until(res["plan"]["id"], {"done", "gaps", "failed"}, wait=30)
    assert p["status"] == "done" and steps[1]["tool"] == "planfn.flaky"
    assert any(k == "choose" for k, _ in model.calls)


def test_tools_he_uses_together_are_proposed_as_a_skill(stack, model, planfn):
    from app import morpho
    from app.main import app
    orch = stack["orch"]
    sql("DELETE FROM mailbox_items WHERE fingerprint LIKE 'together:%'")
    base = datetime.now(timezone.utc) - timedelta(days=3)
    for day, light in enumerate(("Living Room", "Bedroom", "Office")):
        t0 = base + timedelta(days=day)
        for i, (tool, args) in enumerate((("planfn.act", {"what": "dim"}), ("planfn.look", {"q": light}),
                                          ("planfn.act", {"what": "tv on"}))):
            sql("INSERT INTO task_runs (id, tool, args, trigger, user_id, tier, status, started_at) "
                "VALUES ($1, $2, $3::jsonb, 'user', 'owner', 'act', 'completed', $4)",
                uuid.uuid4(), tool, json.dumps(args), t0 + timedelta(minutes=2 * i))

    async def run_together():
        return {"made": await morpho.together(app)}
    _route("/_test/morpho/together", run_together)
    assert httpx.post(f"{orch}/_test/morpho/together").json()["made"] >= 1
    item, prop = card("skill")
    pv = prop["preview"]
    assert [s["tool"] for s in pv["steps"]] == ["planfn.act", "planfn.look", "planfn.act"]
    assert pv["steps"][1]["args"] == {"q": "{q}"} and pv["steps"][0]["args"] == {"what": "dim"}   # what varied is a slot
    assert pv["slots"] == [{"name": "q", "example": "Office"}] and "3 times" in json.loads(item["evidence"])[0]
    assert httpx.post(f"{orch}/_test/morpho/together").json()["made"] == 0          # once


def test_trust_is_earned_used_without_asking_and_taken_back(stack, model, enginefn):
    from app import morpho
    from app.main import app
    orch = stack["orch"]
    sql("DELETE FROM confirmations")
    sql("UPDATE trust_grants SET status='revoked'")
    for _ in range(3):                                                         # test policy: 3 approvals, 0 days
        sql("INSERT INTO confirmations (tool, args_key, args, approved, via) VALUES ('enginefn.confirm', $1, $2::jsonb, true, 'reply')",
            json.dumps({"what": "trusted thing"}, sort_keys=True), json.dumps({"what": "trusted thing"}))
        sql("INSERT INTO confirmations (tool, args_key, args, approved, via) VALUES ('orbit.unlock', 'x', '{}'::jsonb, true, 'reply')")
    from app.memory import args_key
    sql("UPDATE confirmations SET args_key=$1 WHERE tool='enginefn.confirm'", args_key({"what": "trusted thing"}))

    async def run_candidates():
        return {"made": await morpho.candidates(app)}
    _route("/_test/morpho/candidates", run_candidates)
    assert httpx.post(f"{orch}/_test/morpho/candidates").json()["made"] == 1    # never the unlock
    item, prop = card("trust")
    assert prop["tool"] == "enginefn.confirm"
    # before it's granted, it still asks
    r = httpx.post(f"{orch}/tasks", json={"tool": "enginefn.confirm", "args": {"what": "trusted thing"}})
    assert r.status_code == 409
    httpx.post(f"{orch}/mailbox/{item['id']}/decide", json={"action": "approve"}).raise_for_status()
    t, _ = task(orch, "enginefn.confirm", {"what": "trusted thing"})
    assert t["status"] == "completed"
    [run] = sql("SELECT trusted_grant FROM task_runs WHERE id=$1", uuid.UUID(t["id"]))
    assert run["trusted_grant"] == prop["grant_id"]
    assert httpx.post(f"{orch}/tasks", json={"tool": "enginefn.confirm", "args": {"what": "something else"}}).status_code == 409
    t, res = task(orch, "morpho.trusts", {})
    assert res["trusts"]["items"][0]["uses"] >= 1
    # a run that fails takes it back
    bad = uuid.uuid4()
    sql("INSERT INTO task_runs (id, tool, args, trigger, user_id, tier, trusted_grant) VALUES ($1, 'enginefn.confirm', "
        "'{}'::jsonb, 'user', 'owner', 'change', $2)", bad, prop["grant_id"])
    httpx.post(f"{orch}/tasks/{bad}/result", json={"status": "failed", "error": "it broke"}).raise_for_status()
    [g] = sql("SELECT status, why_revoked FROM trust_grants WHERE id=$1", prop["grant_id"])
    assert g["status"] == "revoked" and g["why_revoked"].startswith("it failed")
    assert httpx.post(f"{orch}/tasks", json={"tool": "enginefn.confirm", "args": {"what": "trusted thing"}}).status_code == 409


def test_a_no_takes_trust_back_and_so_do_his_words(stack, model, enginefn):
    from app.memory import args_key
    orch = stack["orch"]
    key = args_key({"what": "trusted thing"})
    sql("UPDATE trust_grants SET status='revoked' WHERE status IN ('active','proposed')")
    g = sql("INSERT INTO trust_grants (tool, args_key, args, title, status, granted_at) VALUES ('enginefn.confirm', $1, "
            "'{\"what\": \"trusted thing\"}'::jsonb, 'enginefn.confirm (what: trusted thing)', 'active', now()) RETURNING id", key)[0]["id"]
    t, res = task(orch, "morpho.revoke", {"which": "trusted thing"})
    assert t["status"] == "completed" and res["headline"] == "I'll ask again first"
    assert sql("SELECT status FROM trust_grants WHERE id=$1", g)[0]["status"] == "revoked"
    g = sql("INSERT INTO trust_grants (tool, args_key, args, title, status, granted_at) VALUES ('enginefn.confirm', $1, "
            "'{\"what\": \"trusted thing\"}'::jsonb, 't', 'active', now()) RETURNING id", key)[0]["id"]
    # he says no to it once (here: through the reply's confirmation card)
    from app import assistant
    import redis as redis_sync
    r = redis_sync.Redis.from_url(REDIS)
    r.set(assistant.PENDING_PREFIX + "abc", json.dumps({"tool": "enginefn.confirm", "args": {"what": "trusted thing"},
                                                         "user": "owner", "reply": None}), ex=60)
    httpx.post(f"{orch}/ask/confirm", json={"action_id": "abc", "approve": False}).raise_for_status()
    assert sql("SELECT status, why_revoked FROM trust_grants WHERE id=$1", g)[0]["why_revoked"] == "you said no to it"
