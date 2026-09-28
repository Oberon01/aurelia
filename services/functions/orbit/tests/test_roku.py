import asyncio
from urllib.parse import unquote, urlsplit

import fakeredis.aioredis
import httpx
import pytest

from app import main
from app.main import ToolError
from app.roku import Roku, RokuError, input_key, key_name

from .test_tools import FakeCtx, FakeHome

SUBNET = ["10.9.9.0/29"]          # 10.9.9.1 - 10.9.9.6
APPS = {"837": "YouTube", "12": "Netflix", "2285": "Hulu", "13": "Prime Video"}


class FakeTv:
    def __init__(self, serial="X1", name="", location="Living Room", power="PowerOn", refuse=False):
        self.serial, self.name, self.location, self.power, self.refuse = serial, name, location, power, refuse
        self.active = ("", "Roku")               # (id, name); "" = home screen
        self.media = "play"
        self.pressed: list[str] = []
        self.launched: list[str] = []
        # As a Roku TV reports them; HDMI 2 renamed "PC" on the TV.
        self.inputs = {"tvinput.hdmi1": "HDMI 1", "tvinput.hdmi2": "PC", "tvinput.dtv": "Live TV"}

    def info(self):
        return (f"<device-info><serial-number>{self.serial}</serial-number><is-tv>true</is-tv>"
                f"<user-device-name>{self.name}</user-device-name>"
                f"<user-device-location>{self.location}</user-device-location>"
                f"<friendly-model-name>TCL Roku TV</friendly-model-name>"
                f"<power-mode>{self.power}</power-mode></device-info>")


class FakeNet:
    """Roku TVs at LAN addresses; anything else doesn't answer."""

    def __init__(self, **tvs: FakeTv):
        self.at = dict(tvs)             # ip -> tv

    def move(self, old: str, new: str):
        self.at[new] = self.at.pop(old)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        tv = self.at.get(url.hostname)
        if tv is None:
            raise httpx.ConnectError("no route", request=request)
        if tv.refuse:
            return httpx.Response(403)
        path = url.path
        if path == "/query/device-info":
            return httpx.Response(200, text=tv.info())
        if path == "/query/apps":
            return httpx.Response(200, text="<apps>" + "".join(
                f'<app id="{i}" type="appl">{n}</app>' for i, n in APPS.items()) + "".join(
                f'<app id="{i}" type="tvin">{n}</app>' for i, n in tv.inputs.items()) + "</apps>")
        if path == "/query/active-app":
            app_id, name = tv.active
            attr = f' id="{app_id}"' if app_id else ""
            return httpx.Response(200, text=f"<active-app><app{attr}>{name}</app></active-app>")
        if path == "/query/media-player":
            return httpx.Response(200, text=f'<player error="false" state="{tv.media}"/>')
        if path.startswith("/keypress/"):
            key = unquote(path.removeprefix("/keypress/"))
            tv.pressed.append(key)
            if key == "PowerOn":
                tv.power = "PowerOn"
            elif key == "PowerOff":
                tv.power = "DisplayOff"
            elif key == "InputHDMI2":
                tv.active = ("tvinput.hdmi2", "HDMI 2")
            elif key == "Play":
                tv.media = "pause" if tv.media == "play" else "play"
            return httpx.Response(200)
        if path.startswith("/launch/"):
            app_id = path.removeprefix("/launch/")
            tv.launched.append(app_id + (f"?{url.query}" if url.query else ""))
            tv.active = (app_id, {**APPS, **tv.inputs}[app_id])
            return httpx.Response(200)
        return httpx.Response(404)


def make_roku(net: FakeNet, redis=None) -> Roku:
    return Roku(redis or fakeredis.aioredis.FakeRedis(), transport=httpx.MockTransport(net.handler),
                probe_timeout=0.2, settle_seconds=0)


# ---- discovery -----------------------------------------------------------------------

def test_sweep_finds_tv_by_serial_and_remembers_it():
    net = FakeNet(**{"10.9.9.4": FakeTv()})
    redis = fakeredis.aioredis.FakeRedis()

    async def go():
        roku = make_roku(net, redis)
        found = await roku.sweep(SUBNET)
        assert [(t.serial, t.ip, t.name) for t in found] == [("X1", "10.9.9.4", "Living Room TV")]
        again = make_roku(net, redis)
        await again.load()                                   # after a restart
        assert again.all()[0].ip == "10.9.9.4"
    asyncio.run(go())


def test_tv_that_changed_ip_is_followed():
    net = FakeNet(**{"10.9.9.4": FakeTv()})

    async def go():
        roku = make_roku(net)
        tv = (await roku.sweep(SUBNET))[0]
        net.move("10.9.9.4", "10.9.9.6")                     # router handed out a new address
        moved = await roku.reach(tv, SUBNET)
        assert moved.ip == "10.9.9.6" and moved.serial == "X1"
        assert len(roku.all()) == 1                          # same TV, not a second one
    asyncio.run(go())


def test_another_device_taking_the_old_ip_is_not_mistaken_for_the_tv():
    net = FakeNet(**{"10.9.9.4": FakeTv(serial="X1")})

    async def go():
        roku = make_roku(net)
        tv = (await roku.sweep(SUBNET))[0]
        net.at = {"10.9.9.4": FakeTv(serial="OTHER", location="Bedroom"), "10.9.9.5": net.at["10.9.9.4"]}
        assert (await roku.reach(tv, SUBNET)).ip == "10.9.9.5"
    asyncio.run(go())


def test_unreachable_tv_explains_fast_tv_start():
    net = FakeNet(**{"10.9.9.4": FakeTv()})

    async def go():
        roku = make_roku(net)
        tv = (await roku.sweep(SUBNET))[0]
        net.at.clear()
        with pytest.raises(RokuError, match="Fast TV Start"):
            await roku.reach(tv, SUBNET)
    asyncio.run(go())


def test_limited_network_access_is_explained():
    net = FakeNet(**{"10.9.9.4": FakeTv(refuse=True)})
    with pytest.raises(RokuError, match="Network access to Enabled"):
        asyncio.run(make_roku(net).sweep(SUBNET))


def test_key_and_input_words():
    assert key_name("volume up") == "VolumeUp" and key_name("OK") == "Select" and key_name("Home") == "Home"
    with pytest.raises(RokuError, match="don't know the remote button"):
        key_name("teleport")
    assert input_key("HDMI 2") == "InputHDMI2" and input_key("antenna") == "InputTuner"
    assert input_key("the PC", {"pc": "hdmi3"}) == "InputHDMI3"
    with pytest.raises(RokuError, match="Your names: pc"):
        input_key("xbox", {"pc": "hdmi3"})


# ---- tools ---------------------------------------------------------------------------

@pytest.fixture
def tv(monkeypatch):
    t = FakeTv(power="DisplayOff")
    net = FakeNet(**{"10.9.9.3": t})
    main.app.state.home = FakeHome([])
    main.app.state.aliases = __import__("app.aliases", fromlist=["AliasStore"]).AliasStore(
        fakeredis.aioredis.FakeRedis())
    main.app.state.roku = make_roku(net)
    monkeypatch.setattr(main, "POLICY", {"policy": {"rules": {
        "heads_up_seconds": 0, "tv": {"subnets": SUBNET, "inputs": {"xbox": "hdmi1"}}}}})
    return t


def run(tool, args):
    return asyncio.run(tool(FakeCtx(args)))


def test_power_on_is_confirmed(tv):
    out = run(main.tv_power, {"name": "the living room tv", "on": True})
    assert out["on"] is True and tv.pressed == ["PowerOn"]
    assert "already on" in run(main.tv_power, {"on": True})["message"]
    assert run(main.tv_power, {"on": False})["on"] is False


def test_switch_to_the_pc_wakes_the_tv_first(tv):
    out = run(main.tv_input, {"input": "the pc"})                 # the TV's own name for HDMI 2
    assert tv.pressed == ["PowerOn"] and tv.launched == ["tvinput.hdmi2"] and out["input"] == "PC"


def test_inputs_follow_what_the_tv_calls_them(tv):
    assert run(main.tv_input, {"input": "hdmi 2"})["input"] == "PC"          # standard name still works
    assert run(main.tv_input, {"input": "the xbox"})["input"] == "HDMI 1"     # your name from orbit.yaml
    assert run(main.tv_input, {"input": "antenna"})["input"] == "Live TV"
    tv.inputs["tvinput.hdmi2"] = "Gaming PC"                                  # renamed on the TV later
    assert run(main.tv_input, {"input": "gaming pc"})["input"] == "Gaming PC"
    with pytest.raises(ToolError, match="Its inputs: HDMI 1, Gaming PC, Live TV"):
        run(main.tv_input, {"input": "hdmi 4"})


def test_list_inputs_and_apps_separately(tv):
    tv.power = "PowerOn"
    run(main.tv_input, {"input": "pc"})
    out = run(main.tv_inputs, {})
    assert [(i["name"], i["port"], i["showing"]) for i in out["inputs"]] == [
        ("HDMI 1", "hdmi1", False), ("PC", "hdmi2", True), ("Live TV", "dtv", False)]
    assert out["your_names"] == {"xbox": "hdmi1"}
    assert "PC" not in run(main.tv_apps, {})["apps"]


def test_open_app_and_a_specific_youtube_video(tv):
    out = run(main.tv_app, {"app": "youtube", "content_id": "dQw4w9WgXcQ"})
    assert out["app"] == "YouTube" and tv.launched == ["837?contentId=dQw4w9WgXcQ"]
    assert run(main.tv_app, {"app": "prime"})["app"] == "Prime Video"
    with pytest.raises(ToolError, match="no app called 'Disney'"):
        run(main.tv_app, {"app": "Disney"})


def test_remote_presses_repeats_and_types(tv):
    tv.power = "PowerOn"
    out = run(main.tv_remote, {"keys": ["volume up"], "repeat": 3})
    assert tv.pressed == ["VolumeUp"] * 3 and out["pressed"] == ["volume up"] * 3
    tv.pressed.clear()
    run(main.tv_remote, {"keys": "search", "text": "lo fi"})
    assert tv.pressed == ["Search", "Lit_l", "Lit_o", "Lit_ ", "Lit_f", "Lit_i"]


def test_pause_never_unpauses(tv):
    tv.power, tv.media = "PowerOn", "pause"
    assert run(main.tv_remote, {"keys": ["pause"]})["message"] == "Already paused."
    assert tv.pressed == []
    run(main.tv_remote, {"keys": ["resume"]})
    assert tv.pressed == ["Play"] and tv.media == "play"


def test_remote_on_a_tv_that_is_off_says_so(tv):
    with pytest.raises(ToolError, match="is off"):
        run(main.tv_remote, {"keys": ["home"]})


def test_tv_nickname_and_status_without_smartrent(tv):
    main.app.state.home = FakeHome([], state="needs_2fa", message="Tell Aurelia the code.")
    run(main.tv_discover, {})                                 # what startup does
    run(main.alias, {"alias": "the big screen", "device": "living room tv"})
    assert run(main.tv_power, {"name": "big screen", "on": True})["name"] == "Living Room TV"
    out = run(main.status, {})
    assert out["devices"][0]["kind"] == "tv" and "Not connected" in out["smartrent"]


def test_discover_tool_lists_what_it_found(tv):
    assert run(main.tv_discover, {})["found"][0]["name"] == "Living Room TV"
