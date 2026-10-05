"""The real config/aurelia.yaml must load: a bad fast path makes the whole file invalid
(Chrysalis would refuse the release, and a running orchestrator would keep the old one)."""
import re
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve()
CONFIG = HERE.parents[3] / "config" / "aurelia.yaml" if len(HERE.parents) > 3 else HERE.parent / "_none"


@pytest.mark.skipif(not CONFIG.exists(), reason="the repo's config isn't in the test image")
def test_the_config_file_loads_and_every_fast_path_compiles():
    from app.config import RuntimeConfig
    cfg = RuntimeConfig.model_validate(yaml.safe_load(CONFIG.read_text()))
    names = [p.name for p in cfg.fastpath.patterns]
    assert len(names) == len(set(names)), "two fast paths share a name"
    for p in cfg.fastpath.patterns:
        re.compile(p.match)


@pytest.mark.skipif(not CONFIG.exists(), reason="the repo's config isn't in the test image")
@pytest.mark.parametrize("said,tool,args", [
    ("what's on the TV PC screen", "constellation.look", {"target": "TV PC"}),
    ("click 3", "constellation.click", {"n": "3", "target": ""}),
    ("press 4 on the tv pc", "constellation.click", {"n": "4", "target": "tv pc"}),
    ("type cats and search", "constellation.type", {"text": "cats", "n": "", "enter": "true"}),
    ("type hello mom into 8", "constellation.type", {"text": "hello mom", "n": "8"}),
    ('type "Hello Mom"', "constellation.type", {"text": "Hello Mom"}),
    ("type 2 diabetes symptoms", None, None),                      # not a command: the model decides
    ("scroll to the top", "constellation.scroll", {"direction": "to the top", "target": ""}),
    ("go back", "constellation.back", {"target": ""}),
    ("go back home on the TV PC", "constellation.home", {"target": "TV PC"}),
    ("go back 10 seconds", "constellation.media", {"action": "back", "amount": "10 seconds", "target": ""}),
    ("skip ahead 30 seconds", "constellation.media", {"action": "forward", "amount": "30 seconds", "target": ""}),
    ("go to 1:30", "constellation.media", {"action": "seek", "amount": "1:30", "target": ""}),
    ("pause", "constellation.media", {"action": "pause", "target": ""}),
    ("full screen on the tv pc", "constellation.media", {"action": "fullscreen", "target": "tv pc"}),
    ("pause the living room tv", "orbit.tv_remote", {"name": "living room tv", "keys": "pause"}),   # Roku keeps its own
    ("mute", "orbit.tv_mute", {"name": "", "muted": "true"}),
    ("volume up", "orbit.tv_remote", {"name": "", "keys": "volume_up", "repeat": "3"}),
])
def test_screen_phrases_reach_the_right_tool(said, tool, args):
    from app import fastpath
    from app.config import RuntimeConfig
    cfg = RuntimeConfig.model_validate(yaml.safe_load(CONFIG.read_text()))
    m = fastpath.match(said, cfg.fastpath.patterns)
    if tool is None:
        assert m is None or not m.pattern.tool.startswith("constellation.")
        return
    assert m and m.pattern.tool == tool and m.args == args


@pytest.mark.skipif(not CONFIG.exists(), reason="the repo's config isn't in the test image")
@pytest.mark.parametrize("said,tool,args", [
    ("how's the tv pc doing", "constellation.health", {"target": "tv pc"}),
    ("why is the laptop so slow", "constellation.health", {"target": "laptop"}),
    ("what are the specs of the desktop", "constellation.health", {"target": "desktop"}),
    ("what's using the memory on the laptop", "constellation.list_processes", {"target": "laptop", "sort": "memory"}),
    ("close chrome on the laptop", "constellation.close_process", {"name": "chrome", "target": "laptop"}),
    ("force close chrome on the laptop", "constellation.close_process", {"name": "chrome", "target": "laptop", "force": "true"}),
    ("close that on the tv pc", "constellation.home", {"target": "tv pc"}),
    ("close the panels on the tv", "constellation.clear", {"target": "tv", "panels": ""}),
    ("close all the panels on my phone", "constellation.clear", {"target": "phone", "panels": ""}),
    ("close panel 2 on the tv", "constellation.clear", {"target": "tv", "panels": "2"}),
    ("close panels 2 and 3 on the TV PC", "constellation.clear", {"target": "TV PC", "panels": "2 and 3"}),
    ("clear the tv's screen", "constellation.clear", {"target": "tv"}),
    ("close everything on the tv", "constellation.clear", {"target": "tv", "everything": "true"}),
    ("close the text editor on the tv", "constellation.close_process", {"name": "text editor", "target": "tv"}),
    ("how's the weather", None, None),
    ("sign in to youtube on the tv pc", "constellation.sign_in", {"site": "youtube", "target": "tv pc"}),
    ("log me into crunchyroll on the TV PC", "constellation.sign_in", {"site": "crunchyroll", "target": "TV PC"}),
])
def test_health_phrases_reach_the_right_tool(said, tool, args):
    from app import fastpath
    from app.config import RuntimeConfig
    cfg = RuntimeConfig.model_validate(yaml.safe_load(CONFIG.read_text()))
    m = fastpath.match(said, cfg.fastpath.patterns)
    if tool is None:
        assert m is None or not m.pattern.tool.startswith("constellation.")
        return
    assert m and m.pattern.tool == tool and m.args == args


@pytest.mark.skipif(not CONFIG.exists(), reason="the repo's config isn't in the test image")
@pytest.mark.parametrize("said,tool,args", [
    ("what can I watch?", "quasar.find", {}),
    ("Aurelia, what can we watch", "quasar.find", {}),
    ("what's there to watch", "quasar.find", {}),
    ("show me my video library", "quasar.find", {}),
    ("open jellyfin", "quasar.find", {}),
    ("find frieren in my jellyfin", "quasar.find", {"query": "frieren"}),
    ("what's in my library", "kepler.recall", {}),                      # the research library stays Kepler's
    ("show my movies", "quasar.find", {"kind": "movies"}),
    ("what shows do I have?", "quasar.find", {"kind": "shows"}),
    ("show me all my tv shows", "quasar.find", {"kind": "shows"}),
    ("show my music", "quasar.find", {"kind": "music"}),
    ("what's on my clipboard?", "constellation.clipboard", {}),
    ("play Frieren on node-tv", "quasar.play", {"what": "Frieren", "on": "node-tv"}),
    ("put on Big Buck", "quasar.play", {"what": "Big Buck", "on": ""}),
    ("watch Dune on the living room TV", "quasar.play", {"what": "Dune", "on": "living room TV"}),
    ("play stranger things on netflix on the tv", "quasar.play", {"what": "stranger things", "on": "netflix on the tv"}),
    ("play the bear on hulu", "quasar.play", {"what": "the bear", "on": "hulu"}),
    ("show me my youtube", "quasar.youtube", {"feed": "home"}),
    ("youtube subscriptions", "quasar.youtube", {"feed": "subscriptions"}),
    ("anything new from my subscriptions", "quasar.youtube", {"feed": "subscriptions"}),
    ("my watch later", "quasar.youtube", {"feed": "watch_later"}),
    ("search lofi beats on youtube", "quasar.youtube", {"feed": "search", "query": "lofi beats"}),
    ("youtube lofi beats", "quasar.youtube", {"feed": "search", "query": "lofi beats"}),
    ("play the youtube video yt:dQw4w9WgXcQ on the desk", "quasar.play", {"id": "yt:dQw4w9WgXcQ", "on": "desk"}),
    ("play lofi beats on youtube", "quasar.play", {"what": "lofi beats", "on": "youtube"}),
    ("play discovery on spotify on the tv", "quasar.play", {"what": "discovery", "on": "spotify on the tv"}),
    ("pause spotify", "quasar.spotify", {"action": "pause"}),
    ("skip the song on spotify", "quasar.spotify", {"action": "skip"}),
    ("what's playing on spotify", "quasar.spotify", {"action": "now"}),
    ("spotify volume 40", "quasar.spotify", {"action": "volume", "level": "40"}),
    ("my spotify speakers", "quasar.spotify", {"action": "devices"}),
    ("send me a test notification", "constellation.notify", {"action": "test"}),
    ("which devices have notifications on", "constellation.notify", {"action": "status"}),
    ("play the album Discovery", "quasar.play", {"what": "the album Discovery", "on": ""}),
    ("play", "constellation.media", {"action": "play", "target": ""}),
    ("pause the video on the tv", "constellation.media", {"action": "pause", "target": "tv"}),
    ("next episode", "constellation.media", {"action": "next", "target": ""}),
    ("play the next episode on the TV", "constellation.media", {"action": "next", "target": "TV"}),
    ("stop", "constellation.media", {"action": "stop", "target": ""}),
    ("stop the movie on node-tv", "constellation.media", {"action": "stop", "target": "node-tv"}),
    ("go home", "constellation.home", {}),
    ("exam mode on the desk for 2 hours", "constellation.mode", {"mode": "exam", "target": "desk", "hours": "2"}),
    ("start exam mode on my desk", "constellation.mode", {"mode": "exam", "target": "desk", "hours": "3"}),
    ("gaming mode on the desk", "constellation.mode", {"mode": "gaming", "target": "desk"}),
    ("end gaming mode", "constellation.mode", {"mode": "normal", "target": ""}),
    ("watch the sign-in", None, None),
    ("copy what's on the desk", "constellation.copy", {"from": "desk"}),
    ("put it on the TV PC", "constellation.paste", {"to": "TV PC"}),
    ("paste that to my phone", "constellation.paste", {"to": "phone"}),
])
def test_library_phrases_reach_the_right_tool(said, tool, args):
    from app import fastpath
    from app.config import RuntimeConfig
    cfg = RuntimeConfig.model_validate(yaml.safe_load(CONFIG.read_text()))
    m = fastpath.match(said, cfg.fastpath.patterns)
    if tool is None:
        assert m is None or m.pattern.tool != "quasar.play", m.pattern.name
        return
    assert m and m.pattern.tool == tool and m.args == args, (said, m and m.pattern.name, m and m.args)


@pytest.mark.skipif(not CONFIG.exists(), reason="the repo's config isn't in the test image")
@pytest.mark.parametrize("said,tool,args", [
    ("make a plan to find the best computer within a $1000 budget", "admiral.plan",
     {"goal": "find the best computer within a $1000 budget"}),
    ("put together a plan for my trip to Chicago", "admiral.plan", {"goal": "my trip to Chicago"}),
    ("plan: compare the two cheapest NAS boxes", "admiral.plan", {"goal": "compare the two cheapest NAS boxes"}),
    ("show my plans", "admiral.plans", {}),
    ("how's the plan going?", "admiral.plans", {}),
    ("stop the plan", "admiral.stop", {}),
    ("cancel my plan", "admiral.stop", {}),
    ("what's the plan for dinner", None, None),
])
def test_plan_phrases_reach_admiral(said, tool, args):
    from app import fastpath
    from app.config import RuntimeConfig
    cfg = RuntimeConfig.model_validate(yaml.safe_load(CONFIG.read_text()))
    m = fastpath.match(said, cfg.fastpath.patterns)
    if tool is None:
        assert m is None or not m.pattern.tool.startswith("admiral.")
        return
    assert m and m.pattern.tool == tool and m.args == args


@pytest.mark.skipif(not CONFIG.exists(), reason="the repo's config isn't in the test image")
@pytest.mark.parametrize("said,hit", [
    ("check my monthly cap", True), ("how much of my budget is left", True), ("when does my cap reset", True),
    ("when does the monthly budget refresh?", True), ("token usage", True), ("what's my claude usage", True),
    ("show me my model budget", True), ("how many tokens have I used this month", True), ("monthly limit", True),
    ("what's my budget for groceries", False), ("check the budget spreadsheet", False),
])
def test_budget_phrases_open_the_budget_panel(said, hit):
    from app import fastpath
    from app.config import RuntimeConfig
    cfg = RuntimeConfig.model_validate(yaml.safe_load(CONFIG.read_text()))
    m = fastpath.match(said, cfg.fastpath.patterns)
    assert bool(m and m.pattern.tool == "mnemosyne.budget") == hit, said


@pytest.mark.skipif(not CONFIG.exists(), reason="the repo's config isn't in the test image")
@pytest.mark.parametrize("said,name", [
    ("remind me at 5 to call the pharmacy", "schedule-remind"),
    ("at 11 PM, lock the front door", "schedule-time-first"),
    ("in 20 minutes turn off the lights", "schedule-time-first"),
    ("tomorrow at 8, make a plan to compare NAS prices", "schedule-time-first"),
    ("turn off the lights in 20 minutes", "schedule-time-last"),      # never "turn off the lights" now
    ("lock the front door at 11pm", "schedule-time-last"),
    ("play music in 20 minutes", "schedule-time-last"),
    ("what's scheduled", "schedule-list"), ("show my reminders", "schedule-list"),
    ("cancel the 11 pm one", "schedule-cancel"), ("cancel my reminder to call mom", "schedule-cancel"),
    ("cancel the plan", "plan-stop"),
    ("what's on tv at 8 pm", None), ("at 5 pm", None), ("in 5 minutes", None), ("tomorrow's weather", None),
])
def test_later_phrases_go_to_the_engine_first(said, name):
    from app import fastpath
    from app.config import RuntimeConfig
    cfg = RuntimeConfig.model_validate(yaml.safe_load(CONFIG.read_text()))
    m = fastpath.match(said, cfg.fastpath.patterns)
    if name is None:
        assert m is None or not m.pattern.name.startswith("schedule-"), said
    else:
        assert m and m.pattern.name == name, (said, m and m.pattern.name)


# Every action has a fast path, so nothing's out of reach when the model isn't (and the common ones cost nothing).
GOVERNANCE = CONFIG.parent / "governance" / "functions"
NO_FAST_PATH = {
    "nebula.youtube": "covered by quasar.youtube (\"youtube X\" searches through it)",
    "orbit.tv_play": "covered by quasar.play, which plays a title on a Roku TV through it",
    "constellation.show": "a routine's or automation's step; said aloud, \"send my youtube to my phone\" does the "
                          "same by fast path (a look-up shown on another display)",
    # Planned in governance, not built yet: when one is built, it needs a fast path (this test says so).
    "constellation.search_files": "planned (Kuiper)", "constellation.browser_history": "planned",
    "constellation.read_file": "planned (Kuiper)", "constellation.launch": "planned",
    "constellation.move_file": "planned (Kuiper)", "constellation.delete_file": "planned (Kuiper)",
    "nebula.submit": "planned", "nebula.upload": "planned",
    "pulsar.save": "only his confirmation of a read-back runs it (never a phrase, never the model)",
}


@pytest.mark.skipif(not CONFIG.exists(), reason="the repo's config isn't in the test image")
def test_every_governed_tool_has_a_fast_path():
    from app.config import RuntimeConfig
    cfg = RuntimeConfig.model_validate(yaml.safe_load(CONFIG.read_text()))
    have = {p.tool for p in cfg.fastpath.patterns}
    missing = []
    for f in sorted(GOVERNANCE.glob("*.yaml")):
        g = yaml.safe_load(f.read_text())
        for tool, rule in (g.get("tools") or {}).items():
            q = f"{g['function']}.{tool}"
            if rule.get("tier") != "never" and q not in have and q not in NO_FAST_PATH:
                missing.append(q)
    assert not missing, f"no fast path for: {missing}"


@pytest.mark.skipif(not CONFIG.exists(), reason="the repo's config isn't in the test image")
@pytest.mark.parametrize("said,tool,args", [
    # computers
    ("turn off the screen on the desk", "constellation.power", {"action": "screen_off", "target": "desk"}),
    ("turn the laptop's screen on", "constellation.power", {"action": "screen_on", "target": "laptop"}),
    ("lock the laptop", "constellation.power", {"action": "lock", "target": "laptop"}),
    ("lock the screen on the tv pc", "constellation.power", {"action": "lock", "target": "tv pc"}),
    ("lock the desk's screen", "constellation.power", {"action": "lock", "target": "desk"}),
    ("lock the front door", "orbit.lock", {"name": "front"}),
    ("wake the desk", "constellation.wake", {"target": "desk"}),
    ("wake up the tv pc", "constellation.wake", {"target": "tv pc"}),
    ("turn on the desk computer", "constellation.wake", {"target": "desk computer"}),
    ("turn on the living room light", "orbit.light", {"name": "living room", "on": "on"}),
    ("put the laptop to sleep", "constellation.power", {"action": "sleep", "target": "laptop"}),
    ("how long has the desk been idle", "constellation.power", {"action": "idle", "target": "desk"}),
    ("restart the tv pc", "constellation.restart", {"action": "restart", "target": "tv pc"}),
    ("reboot my laptop", "constellation.restart", {"action": "restart", "target": "laptop"}),
    ("shut down the desk", "constellation.restart", {"action": "shutdown", "target": "desk"}),
    ("restart the video", "constellation.media", {"action": "restart", "target": ""}),
    ("what's wrong with the tv pc", "constellation.diagnose", {"target": "tv pc"}),
    ("show me the TV PC screen", "constellation.watch", {"target": "TV PC"}),
    ("let me see what you're doing", "constellation.watch", {"target": ""}),
    ("what's on my screens", "constellation.screens", {}),
    ("send panel 2 to the tv", "constellation.send", {"panel": "2", "to": "tv"}),
    ("send this to my phone", "constellation.send", {"panel": "", "to": "phone"}),
    ("send https://example.com/a to the desk", "constellation.send", {"url": "https://example.com/a", "to": "desk"}),
    ("send a note to the tv saying dinner is ready", "constellation.send", {"text": "dinner is ready", "to": "tv"}),
    ("put it on the TV PC", "constellation.paste", {"to": "TV PC"}),
    ("set the sound on the desk to 30", "constellation.sound", {"action": "set", "level": "30", "target": "desk"}),
    ("sound up on the laptop", "constellation.sound", {"action": "up", "target": "laptop"}),
    ("mute the sound on the desk", "constellation.sound", {"action": "mute", "target": "desk"}),
    ("how loud is the laptop", "constellation.sound", {"action": "get", "target": "laptop"}),
    ("what speakers does the desk have", "constellation.sound", {"action": "outputs", "target": "desk"}),
    ("play the sound through the headphones on the desk", "constellation.sound",
     {"action": "output", "device": "headphones", "target": "desk"}),
    ("submit the form on the tv pc", "constellation.submit", {"target": "tv pc"}),
    ("what sites are saved on the tv pc", "constellation.logins", {"target": "tv pc"}),
    ("kill the process python on the desk", "constellation.kill_process", {"name": "python", "target": "desk"}),
    ("make the tv pc a spotify speaker", "quasar.speaker", {"target": "tv pc"}),
    ("install librespot on the laptop", "quasar.speaker", {"target": "laptop"}),
    ("install vlc on the tv pc", "constellation.install", {"app": "vlc", "target": "tv pc"}),
    ("install the Ubuntu package vlc on the tv pc", "constellation.install_package",
     {"name": "vlc", "source": "apt", "target": "tv pc"}),
    ("install the snap spotify on the tv pc", "constellation.install_package",
     {"name": "spotify", "source": "snap", "target": "tv pc"}),
    ("remove the Ubuntu package vlc on the tv pc", "constellation.remove_package",
     {"name": "vlc", "source": "apt", "target": "tv pc"}),
    ("uninstall vlc from the laptop", "constellation.uninstall", {"app": "vlc", "target": "laptop"}),
    ("remove panel 2 from the tv", "constellation.clear", {"target": "tv", "panels": "2"}),
    # releases
    ("roll back the last release", "chrysalis.rollback", {}),
    ("approve release 42", "chrysalis.approve", {"release_id": "42"}),
    # learning and the record
    ("explain BGP route reflectors", "kepler.explain", {"subject": "BGP route reflectors"}),
    ("read the pdf at https://example.com/a.pdf", "kepler.read_pdf", {"url": "https://example.com/a.pdf"}),
    ("read the file az-104 notes.pdf", "kepler.read_pdf", {"file": "az-104 notes.pdf"}),
    ("what did we decide about the thermostat", "mnemosyne.recall", {"query": "the thermostat"}),
    ("what were we talking about yesterday", "mnemosyne.recall", {"date": "yesterday"}),
    ("forget fact 12", "mnemosyne.forget", {"fact_id": "12"}),
    ("forget conversation 7", "mnemosyne.forget", {"record_id": "7"}),
    ("what changed in the last release", "mnemosyne.history", {"release": "latest"}),
    ("what's new", "mnemosyne.history", {"since": "this week"}),
    ("what's next", "mnemosyne.roadmap", {}),
    ("show me the roadmap", "mnemosyne.roadmap", {}),
    ("tell me about phase 10", "mnemosyne.roadmap", {"phase": "10"}),
    # the home
    ("show my nicknames", "orbit.aliases", {}),
    ("forget the nickname big light", "orbit.forget_alias", {"alias": "big light"}),
    ("is the tv on", "orbit.tv_status", {"name": "tv"}),
    ("what roku apps are there", "orbit.tv_apps", {"name": ""}),
    ("what inputs does the tv have", "orbit.tv_inputs", {"name": "tv"}),
    ("find my tvs", "orbit.tv_discover", {}),
])
def test_every_action_phrase_reaches_its_tool(said, tool, args):
    from app import fastpath
    from app.config import RuntimeConfig
    cfg = RuntimeConfig.model_validate(yaml.safe_load(CONFIG.read_text()))
    m = fastpath.match(said, cfg.fastpath.patterns)
    assert m and m.pattern.tool == tool, (said, m and (m.pattern.name, m.pattern.tool))
    assert {k: v for k, v in m.args.items()} == args, (said, m.args)


@pytest.mark.skipif(not CONFIG.exists(), reason="the repo's config isn't in the test image")
@pytest.mark.parametrize("said,tool,args", [
    ("show my skills", "morpho.skills", {}), ("what skills do you have", "morpho.skills", {}),
    ("keep that as a skill", "morpho.keep", {}),
    ("run the skill movie night", "morpho.run", {"skill": "movie night"}),
    ("retire the skill movie night", "morpho.retire", {"skill": "movie night"}),
    ("roll back the skill best computer", "morpho.rollback", {"skill": "best computer"}),
    ("roll back the last release", "chrysalis.rollback", {}),
    ("what do you do without asking", "morpho.trusts", {}),
    ("ask me again before restarting the desk", "morpho.revoke", {"which": "restarting the desk"}),
])
def test_morpho_phrases(said, tool, args):
    from app import fastpath
    from app.config import RuntimeConfig
    cfg = RuntimeConfig.model_validate(yaml.safe_load(CONFIG.read_text()))
    m = fastpath.match(said, cfg.fastpath.patterns)
    assert m and m.pattern.tool == tool and m.args == args, (said, m and (m.pattern.name, m.args))


@pytest.mark.skipif(not CONFIG.exists(), reason="the repo's config isn't in the test image")
@pytest.mark.parametrize("said,tool", [
    ("every weekday at 7 am lock the front door", "pulsar.automate"),
    ("lock the front door every night at 11 pm", "pulsar.automate"),
    ("turn off the living room light every night at 11", "pulsar.automate"),
    ("remind me every sunday at 6 pm to take the trash out", "pulsar.automate"),
    ("remind me to take the trash out every sunday at 6 pm", "pulsar.automate"),
    ("on weekdays at 7 am turn on the living room light", "pulsar.automate"),
    ("lock the door nightly", "pulsar.automate"),
    ("on the 1st of every month at 9 am remind me to pay rent", "pulsar.automate"),
    ("when the tv pc comes online open youtube on the tv pc", "pulsar.automate"),
    ("open youtube on the tv pc when the tv pc comes online", "pulsar.automate"),
    ("make a routine called goodnight: lock the front door and turn off the tv", "pulsar.routine"),
    ("show my automations", "pulsar.automations"), ("my routines", "pulsar.automations"),
    ("pause the lock automation", "pulsar.pause"), ("resume the lock automation", "pulsar.resume"),
    ("skip tomorrow's lock automation", "pulsar.skip"), ("delete the lock automation", "pulsar.delete"),
    ("delete the goodnight routine", "pulsar.forget_routine"), ("run the goodnight routine", "morpho.run"),
    ("any alerts", "pulsar.notices"), ("what are you watching for", "pulsar.notices"),
    # Not automations: they keep their own fast paths (or go to the model).
    ("remind me at 5 to call the pharmacy", "admiral.schedule"), ("at 11 PM, lock the front door", "admiral.schedule"),
    ("check my monthly cap", "mnemosyne.budget"), ("when does my cap reset", "mnemosyne.budget"),
    ("what happens every day", None), ("show the daily digest", None),
])
def test_automations_have_fast_paths_and_take_nothing_else(said, tool):
    from app import fastpath
    from app.config import RuntimeConfig
    cfg = RuntimeConfig.model_validate(yaml.safe_load(CONFIG.read_text()))
    m = fastpath.match(said, cfg.fastpath.patterns)
    got = m.pattern.tool if m else None
    if tool is None:
        assert got is None or not got.startswith("pulsar."), (said, got)
    else:
        assert got == tool, (said, got)


# The HUD's own buttons (the player spot and the day tape) say what he'd say, so each one must hit its fast path:
# no model, and the same checks as when he types it.
@pytest.mark.skipif(not CONFIG.exists(), reason="the repo's config isn't in the test image")
@pytest.mark.parametrize("said,tool,args", [
    ("pause on spotify", "quasar.spotify", {"action": "pause"}),
    ("resume on spotify", "quasar.spotify", {"action": "resume"}),
    ("next on spotify", "quasar.spotify", {"action": "next"}),
    ("previous on spotify", "quasar.spotify", {"action": "previous"}),
    ("pause on the TV PC", "constellation.media", {"action": "pause", "target": "TV PC"}),
    ("play on the TV PC", "constellation.media", {"action": "play", "target": "TV PC"}),
    ("play the next one on the TV PC", "constellation.media", {"action": "next", "target": "TV PC"}),
    ("rewind 10 seconds on the TV PC", "constellation.media", {"action": "back", "target": "TV PC"}),
    ("show my automations", "pulsar.automations", {}),
    ("what's scheduled", "admiral.scheduled", {}),
    ("show my digest", "parallax.digest", {}),
])
def test_the_hud_buttons_say_what_a_fast_path_hears(said, tool, args):
    from app import fastpath
    from app.config import RuntimeConfig
    cfg = RuntimeConfig.model_validate(yaml.safe_load(CONFIG.read_text()))
    m = fastpath.match(said, cfg.fastpath.patterns)
    assert m and m.pattern.tool == tool, (said, m and m.pattern.tool)
    assert {k: m.args.get(k) for k in args} == args, (said, m.args)
    view = (HERE.parents[1] / "app" / "static" / "view.html").read_text()
    words = said.replace("TV PC", "${n.node}")
    assert f'"{words}"' in view or f"`{words}`" in view, f"the HUD no longer says {said!r}"


# Lagrange: connections in his words, and never stealing a phrase that already meant something else.
@pytest.mark.skipif(not CONFIG.exists(), reason="the repo's config isn't in the test image")
@pytest.mark.parametrize("said,tool,args", [
    ("show my connections", "lagrange.connections", {}), ("what's connected", "lagrange.connections", {}),
    ("connect a service", "lagrange.connect_form", {}), ("connect a new mcp server", "lagrange.connect_form", {}),
    ("connect a webhook", "lagrange.connect_form", {"kind": "hook_in"}),
    ("connect home assistant", "lagrange.connect", {"name": "home assistant"}),
    ("connect gitea", "lagrange.connect", {"name": "gitea"}), ("connect the gitea draft", "lagrange.connect", {"name": "gitea"}),
    ("show the gitea connection", "lagrange.connection", {"name": "gitea"}),
    ("connect the spotify draft", "lagrange.connect", {"name": "spotify"}),
    ("approve the new gitea tools", "lagrange.approve", {"name": "gitea"}),
    ("refresh the gitea connection", "lagrange.refresh", {"name": "gitea"}),
    ("check gitea for new tools", "lagrange.refresh", {"name": "gitea"}),
    ("disconnect gitea", "lagrange.disconnect", {"name": "gitea"}),
    ("turn off the gitea connection", "lagrange.turn_off", {"name": "gitea"}),
    ("turn on the gitea connection", "lagrange.turn_on", {"name": "gitea"}),
    ("sign in to gitea again", "lagrange.sign_in", {"name": "gitea"}),
    ("when the front door unlocks during quiet hours, turn on the hall light", "pulsar.automate", {}),
    ("turn on the porch light when I get home", "pulsar.automate", {}),
    ("when the arrive home webhook is called, turn on the porch light", "pulsar.automate", {}),
    ("sign in to youtube on the tv pc", "constellation.screen", None), ("turn off the kitchen light", "orbit.light", None),
    ("connect to the tv", None, None), ("connect my airpods", None, None),
    ("stop using the skill goodnight", "morpho.retire", None), ("refresh the page", None, None),
    ("disconnect my headphones", None, None), ("disconnect from wifi", None, None), ("unlink my phone", None, None),
    ("sign in to my email", None, None), ("show me the dev tools", None, None), ("stop using it", None, None),
    ("refresh gitea", None, None), ("show the gitea tools", None, None),
])
def test_connection_phrases(said, tool, args):
    from app import fastpath
    from app.config import RuntimeConfig
    cfg = RuntimeConfig.model_validate(yaml.safe_load(CONFIG.read_text()))
    m = fastpath.match(said, cfg.fastpath.patterns)
    if tool is None:
        assert m is None or not m.pattern.tool.startswith("lagrange."), (said, m and m.pattern.name)
        return
    if args is None:                                          # one that already meant something else: unchanged
        assert m is None or not m.pattern.tool.startswith("lagrange."), (said, m and m.pattern.tool)
        return
    assert m and m.pattern.tool == tool, (said, m and m.pattern.tool)
    assert {k: m.args.get(k) for k in args} == args, (said, m.args)


# An event automation's conditions reach the drafter whole: a window dropped here would be saved without it.
@pytest.mark.skipif(not CONFIG.exists(), reason="the repo's config isn't in the test image")
@pytest.mark.parametrize("said", [
    "turn on the lights when the tv pc comes online on weekdays",
    "turn on the lights when the tv pc comes online between 5pm and 6pm",
    "turn on the hall light when the front door opens after 9pm",
    "flip the porch light when the front door unlocks between 3pm and 4pm",
    "turn on the light when I get home before 7am on weekends",
    "turn on the porch light when the front door unlocks during quiet hours",
    "when the den pc comes online between 5pm and 6pm, open youtube on the den pc",
    "when the front door unlocks between 5pm and 6pm, run coming home",
])
def test_an_event_automation_keeps_its_conditions(said):
    from app import fastpath
    from app.config import RuntimeConfig
    cfg = RuntimeConfig.model_validate(yaml.safe_load(CONFIG.read_text()))
    m = fastpath.match(said, cfg.fastpath.patterns)
    assert m and m.pattern.tool == "pulsar.automate", (said, m and m.pattern.name)
    assert m.args["say"] == said, (said, m.args)
