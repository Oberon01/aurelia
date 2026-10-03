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
