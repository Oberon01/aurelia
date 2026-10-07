"""Skipper (Phase 12.5): the phone's menu. Every sentence on it reaches its tool by a fast path (never the
model), a bad entry is left out without taking the menu with it, and his details fill the sentence."""
import asyncio
import os
import types
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")
os.environ.setdefault("REDIS_URL", "redis://unused:6379/0")

import pytest  # noqa: E402
import yaml  # noqa: E402

from app import skipper  # noqa: E402

HERE = Path(__file__).resolve()
ROOT = HERE.parents[3] if len(HERE.parents) > 3 else HERE.parent
MENU, CONFIG = ROOT / "config" / "menu.yaml", ROOT / "config" / "aurelia.yaml"


def test_his_details_fill_the_sentence_and_empty_optional_parts_go():
    say = "play {what}[ on the {on}]"
    assert skipper.fill(say, {"what": "Dune", "on": "TV PC"}) == "play Dune on the TV PC"
    assert skipper.fill(say, {"what": "  Dune  part two "}) == "play Dune part two"
    assert skipper.fill("turn {state} the {light}", {"state": "off", "light": "Kitchen light"}) == "turn off the Kitchen light"


def test_a_bad_entry_is_left_out_and_the_rest_stays(tmp_path):
    p = tmp_path / "menu.yaml"
    p.write_text(yaml.safe_dump({"functions": [{"name": "orbit", "label": "Orbit", "commands": [
        {"label": "Good", "say": "lock the {door}", "tool": "orbit.lock", "fields": {"door": {"label": "Door", "pick": "locks"}}},
        {"label": "No tool", "say": "lock up"},
        {"label": "Missing field", "say": "lock the {door}", "tool": "orbit.lock"},
        {"label": "Odd pick", "say": "lock the {door}", "tool": "orbit.lock", "fields": {"door": {"pick": "planets"}}},
        {"label": "Optional outside", "say": "lock the {door}", "tool": "orbit.lock", "fields": {"door": {"optional": True}}},
    ]}]}))
    m = skipper.load(p)
    assert [c["label"] for c in m["functions"][0]["commands"]] == ["Good"]
    assert m["functions"][0]["commands"][0]["fields"] == [{"name": "door", "label": "Door", "pick": "locks"}]
    p.write_text("functions: [")                                    # unreadable: an empty menu, never a crash
    assert skipper.load(p) == {"functions": []}


@pytest.mark.skipif(not (MENU.exists() and CONFIG.exists()), reason="the repo's config isn't in the test image")
def test_every_command_on_the_menu_reaches_its_tool_by_a_fast_path():
    from app.config import RuntimeConfig
    patterns = RuntimeConfig.model_validate(yaml.safe_load(CONFIG.read_text())).fastpath.patterns
    raw = yaml.safe_load(MENU.read_text())
    assert skipper.problems(raw, patterns) == []
    loaded = skipper.load(MENU)
    assert sum(len(f["commands"]) for f in loaded["functions"]) == sum(len(f["commands"]) for f in raw["functions"]) > 80


@pytest.mark.skipif(not CONFIG.exists(), reason="the repo's config isn't in the test image")
def test_a_sentence_that_would_go_to_the_model_or_elsewhere_is_named():
    from app.config import RuntimeConfig
    patterns = RuntimeConfig.model_validate(yaml.safe_load(CONFIG.read_text())).fastpath.patterns
    raw = {"functions": [{"name": "orbit", "commands": [
        {"label": "Vague", "say": "do the thing with the {x}", "tool": "orbit.lock", "fields": {"x": {"placeholder": "door"}}},
        {"label": "Elsewhere", "say": "show my devices", "tool": "orbit.status"},
        {"label": "Fine", "say": "play {what}[ on the {on}]", "tool": "quasar.play",
         "fields": {"what": {"placeholder": "Dune"}, "on": {"pick": "computers", "optional": True}}}]}]}
    got = skipper.problems(raw, patterns)
    assert len(got) == 2 and "would go to the model" in got[0] and "reaches constellation.devices, not orbit.status" in got[1]
    assert skipper.problems({"nope": 1}, patterns) == ["menu.yaml is functions: a list, each with its commands"]


def test_a_connected_service_brings_its_commands_that_need_nothing_more(monkeypatch):
    from app import registry
    tool = lambda name, required=None: types.SimpleNamespace(name=name, description=f"{name} tool",
                                                             input_schema={"required": required} if required else {})
    fns = [types.SimpleNamespace(name="gitea", via="lagrange", tools=[tool("list_my_repos"), tool("get_issue", ["id"]), tool("get_me")]),
           types.SimpleNamespace(name="orbit", via=None, tools=[tool("status")])]

    async def list_all(redis):
        return fns
    monkeypatch.setattr(registry, "list_all", list_all)
    app = types.SimpleNamespace(state=types.SimpleNamespace(redis=None))
    [g] = asyncio.run(skipper._connections(app))
    assert g["label"] == "Gitea" and g["connection"] is True
    assert [(c["label"], c["say"], c["tool"]) for c in g["commands"]] == [("List my repos", "gitea list my repos", "gitea.list_my_repos")]


@pytest.mark.skipif(not CONFIG.exists(), reason="the repo's config isn't in the test image")
def test_the_menu_shows_only_whats_running_and_reachable(monkeypatch, tmp_path):
    from app import registry
    from app.config import RuntimeConfig
    cfg = RuntimeConfig.model_validate(yaml.safe_load(CONFIG.read_text()))
    p = tmp_path / "menu.yaml"
    p.write_text(yaml.safe_dump({"functions": [
        {"name": "orbit", "label": "Orbit", "commands": [{"label": "Lock up", "say": "lock up", "tool": "orbit.lock"},
                                                          {"label": "Gone", "say": "x", "tool": "orbit.gone"},
                                                          {"label": "Wrong words", "say": "please do the lock thing", "tool": "orbit.lock"}]},
        {"name": "beacon", "label": "Beacon", "commands": [{"label": "Email", "say": "show my email", "tool": "beacon.inbox"}]}]}))
    monkeypatch.setattr(skipper, "menu_path", lambda app=None: p)
    skipper._cache.update(menu=None, mtime=None, path=None)

    async def list_all(redis):
        return [types.SimpleNamespace(name="orbit", via=None, tools=[types.SimpleNamespace(name="lock", input_schema={})])]
    monkeypatch.setattr(registry, "list_all", list_all)
    app = types.SimpleNamespace(state=types.SimpleNamespace(redis=None, config=types.SimpleNamespace(current=cfg)))
    out = asyncio.run(skipper.menu(types.SimpleNamespace(app=app)))
    assert [(f["name"], [c["label"] for c in f["commands"]]) for f in out["functions"]] == [("orbit", ["Lock up"])]
