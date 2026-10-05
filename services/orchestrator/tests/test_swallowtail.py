"""Swallowtail's field checks without a database: reading checks files, his names, and judging what happened."""
import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")
os.environ.setdefault("REDIS_URL", "redis://localhost")

import pytest
import yaml

from app import auth, swallowtail as S

HERE = Path(__file__).resolve()
CHECKS = HERE.parents[3] / "config" / "checks" if len(HERE.parents) > 3 else HERE.parent / "_none"
CONFIG = HERE.parents[3] / "config" / "aurelia.yaml" if len(HERE.parents) > 3 else HERE.parent / "_none"


def act(**kw):
    base = {"route": "fastpath", "reply": "", "tools": [], "asked_to_confirm": []}
    return {**base, **kw}


def test_judging_each_expectation_against_what_happened():
    a = act(reply="Every day at 7:00 AM: Lock the door. Save it?", asked_to_confirm=["pulsar.save"],
            tools=[{"tool": "pulsar.automate", "status": "completed", "error": None,
                    "result": {"automation_draft": {"first": "tomorrow"}, "n": 3}}])
    lines = S.judge({"route": "fastpath", "tool": "pulsar.automate", "asks_to_confirm": "pulsar.save",
                     "reply_starts": "Every day", "reply_has": ["Save it?"], "reply_not_has": "couldn't",
                     "status": "completed", "result": {"n": 3, "automation_draft.first": "Tomorrow"},
                     "result_has": {"automation_draft": "tomorrow"}}, a)
    assert all(x["ok"] for x in lines), [x for x in lines if not x["ok"]]
    bad = S.judge({"route": "not fastpath", "tool": "none", "asks_to_confirm": False, "reply_matches": r"^\d+$"}, a)
    assert [x["ok"] for x in bad] == [False, False, False, False]
    assert bad[1] == {"what": "tool it ran", "expected": "none", "actual": "pulsar.automate", "ok": False}
    assert S.judge({"route": "llm", "tool": "none"}, act(route="llm"))[1]["ok"] is True


def test_confirmed_means_the_result_says_it_happened():
    ok = act(tools=[{"tool": "x.open", "status": "completed", "result": {"opened": True}}])
    no = act(tools=[{"tool": "x.open", "status": "completed", "result": {"did": {"unconfirmed": True}}}])
    assert S.judge({"confirmed": True}, ok)[0]["ok"] and not S.judge({"confirmed": True}, no)[0]["ok"]


def test_his_names_and_times_fill_in_and_a_missing_one_is_said():
    names = {"light": "porch light"}
    assert S.fill(None, {"say": "turn on the {light}", "args": [{"x": "{light}"}], "n": 3}, names) == \
        {"say": "turn on the porch light", "args": [{"x": "porch light"}], "n": 3}
    with pytest.raises(S.Missing) as e:
        S.fill(None, "lock the {lock}", names)
    assert str(e.value) == "lock"
    assert S.fill(None, r"^\d{2}$", names) == r"^\d{2}$"           # a regex isn't a name


def test_what_a_check_needs_and_files_that_are_wrong():
    f = S.CheckFile.model_validate(yaml.safe_load("""
release: T
checks:
  - {id: a, name: looks, say: "peek", expect: {tool: x.y}}
  - {id: b, name: acts, say: "flip", acts: true, expect: {tool: x.y}}
  - {id: c, name: asks, needs_you: "do it", question: "did it?"}
  - {id: d, name: model, say: "hi", model: true, expect: {route: llm}}
"""))
    assert [S.needs(c) for c in f.checks] == ["auto", "start", "start", "start"] and not S.validate(f)
    bad = S.CheckFile.model_validate(yaml.safe_load("""
release: T
checks:
  - {id: a, name: x, say: "a", tool: "b.c", expect: {colour: red}}
  - {id: a, name: y, verify: {tool: o.s}}
  - {id: c, name: z, say: "a", expect: {route: sideways}, within: "soon"}
"""))
    problems = " | ".join(S.validate(bad))
    for want in ("say or tool, not both", "unknown expect colour", "appears twice", "verify needs a tool and a field",
                 "route is one of", "isn't a length of time"):
        assert want in problems, want
    with pytest.raises(Exception):
        S.CheckFile.model_validate({"release": "T", "checks": [{"id": "a", "name": "b", "typo": 1}]})
    assert S._seconds("30s") == 30 and S._seconds("6m") == 360 and S._seconds(5) == 5


def test_a_tvs_screen_cant_start_checks_or_answer_them():
    assert not auth.allowed("screen", "/swallowtail/runs/3/start")
    assert not auth.allowed("screen", "/swallowtail/results/9/answer")
    assert auth.allowed("screen", "/swallowtail/runs/3/panel")


@pytest.mark.skipif(not CHECKS.is_dir(), reason="the repo's checks aren't in the test image")
def test_every_checks_file_in_the_repo_is_valid_and_each_says_reaches_its_tool():
    from app import fastpath, when
    from app.config import RuntimeConfig
    cfg = RuntimeConfig.model_validate(yaml.safe_load(CONFIG.read_text()))
    names = {"light": "living room light", "lock": "front door", "tv": "living room tv", "tv_computer": "TV PC"}
    home = yaml.safe_load((CHECKS / S.HOME_FILE).read_text())
    assert set(home["vars"]) == set(names)                          # every name the checks use is in _home.yaml
    files = sorted(p for p in CHECKS.glob("*.yaml") if p.name != S.HOME_FILE)
    assert len(files) >= 8
    for p in files:
        f, problem = S.load_file(p)
        assert f is not None, problem
        for c in f.checks:
            for k in S.VAR.findall(c.model_dump_json()):
                assert k in names or __import__("re").fullmatch(r"in_\d+_minutes?", k), (p.name, k)
            want = c.expect.get("tool")
            if c.say and want and want != "none" and not c.say.startswith("send "):   # (a send is a look-up sent elsewhere)
                text = S.VAR.sub(lambda m: names[m.group(1)], c.say)
                m = fastpath.match(text, cfg.fastpath.patterns)
                assert m is not None and m.pattern.tool == want, (p.name, c.id, m and m.pattern.tool)
                assert not when.mentions_repeat(text) or want.startswith("pulsar."), (p.name, c.id)


def test_chrysalis_checks_a_checks_file_before_its_release_applies():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)                                       # (no lifespan: the endpoint needs none)
    ok = c.post("/config/validate", json={"path": "config/checks/x.yaml", "content":
                                          "release: X\nchecks:\n  - {id: a, name: b, say: hi, expect: {tool: x.y}}\n"}).json()
    assert ok == {"valid": True, "checked": True}
    bad = c.post("/config/validate", json={"path": "config/checks/x.yaml", "content":
                                           "release: X\nchecks:\n  - {id: a, name: b, say: hi, expect: {colour: red}}\n"}).json()
    assert bad["valid"] is False and "unknown expect colour" in " ".join(bad["error"])
    home = c.post("/config/validate", json={"path": "config/checks/_home.yaml", "content": "vars: [1, 2]\n"}).json()
    assert home["valid"] is False
