import asyncio

import pytest

from app import main
from app.devices import DeviceNotFound, describe, resolve
from app.main import ToolError

from .fakes import DoorLock, my_home


class FakeHome:
    def __init__(self, devices, state="ready", message=""):
        self._devices, self.state, self.message = devices, state, message

    def devices(self): return self._devices
    async def raw_devices(self):
        ids = [getattr(d, "_device_id") for d in self._devices]
        return [{"id": i, "name": "?", "type": "x", "attributes": []} for i in ids] + [
            {"id": 99, "name": "Patio Door Contact", "type": "sensor_notification",
             "attributes": [{"name": "open", "state": "false"}]}]


class FakeCtx:
    def __init__(self, args, cancel_after_progress=False):
        self.args, self.progress_calls, self._cancel = args, [], cancel_after_progress
        self.task_id = "t1"

    @property
    def cancelled(self): return self._cancel and bool(self.progress_calls)

    async def progress(self, *a, **k): self.progress_calls.append(a)

    async def heads_up(self, what):
        return await main.TaskContext.heads_up(self, what)


@pytest.fixture
def home(monkeypatch):
    devices = my_home()
    main.app.state.home = FakeHome(devices)
    monkeypatch.setattr(main.settings, "confirm_after_seconds", 0)
    monkeypatch.setattr(main, "POLICY", {"policy": {"rules": {"thermostat": {"min_f": 60, "max_f": 85},
                                                               "heads_up_seconds": 0}}})
    return devices


def call(tool, args, **k):
    return asyncio.run(tool(FakeCtx(args, **k)))


# ---- matching --------------------------------------------------------------------------

def test_resolve_by_partial_name_generic_word_and_ambiguity(home):
    assert resolve(home, "light", "bedroom").get_name() == "Bedroom Lamp"
    assert resolve(home, "lock", "the door").get_name() == "Front Door"       # generic word, one lock
    assert resolve(home, "lock", "front").get_name() == "Front Door"
    with pytest.raises(DeviceNotFound, match="Which light"):
        resolve(home, "light", "lights")                                       # two lights
    with pytest.raises(DeviceNotFound, match="No light matches 'garage'"):
        resolve(home, "light", "garage")


@pytest.mark.parametrize("said", [
    "my living room light", "living room", "the living room lights", "Living Room",
    "living rm light", "livingroom", "light in the living room", "my living room",
])
def test_resolve_the_way_people_actually_say_it(home, said):
    assert resolve(home, "light", said).get_name() == "Living Room Light"


def test_resolve_with_real_world_names_and_aliases():
    from .fakes import BinarySwitch
    devices = [BinarySwitch(1, "Dining Rm Switch"), BinarySwitch(2, "Switch 2")]
    assert resolve(devices, "light", "my dining room light").get_name() == "Dining Rm Switch"
    with pytest.raises(DeviceNotFound):
        resolve(devices, "light", "living room")          # must NOT become "Dining Rm"
    assert resolve(devices, "light", "dinning room").get_name() == "Dining Rm Switch"   # typo
    assert resolve(devices, "light", "the living room light",
                   aliases={"living room": "Switch 2"}).get_name() == "Switch 2"


# ---- tools -----------------------------------------------------------------------------

def test_status_reports_known_and_unmodelled_devices(home):
    out = call(main.status, {})
    kinds = {d["name"]: d["kind"] for d in out["devices"]}
    assert kinds["Front Door"] == "lock" and kinds["Bedroom Lamp"] == "light"
    assert out["devices"][0]["battery"] == 87
    assert out["other_devices"][0]["name"] == "Patio Door Contact"


def test_lock_and_unlock_confirm_real_state(home):
    door = home[0]
    assert call(main.unlock, {})["locked"] is False and door.fetches == 1
    assert call(main.lock, {"name": "front door"})["locked"] is True


def test_jammed_lock_is_reported_not_assumed(home):
    home[0]._locked, home[0].jammed = False, True
    with pytest.raises(ToolError, match="still reports unlocked"):
        call(main.lock, {})


def test_lights_plain_and_dimmable(home):
    assert call(main.light, {"name": "living room", "on": True})["on"] is True
    assert call(main.light, {"name": "bedroom", "level": 100})["level"] == 99        # dimmers top out at 99
    assert call(main.light, {"name": "bedroom", "on": False})["level"] == 0
    assert call(main.light, {"name": "living room", "level": 0})["on"] is False      # plain switch: 0 = off
    with pytest.raises(ToolError, match="on or off"):
        call(main.light, {"name": "bedroom"})


def test_thermostat_target_follows_mode_and_limits(home):
    t = home[3]
    assert call(main.thermostat, {"target": 70})["heat_setpoint"] == 70
    call(main.thermostat, {"mode": "cool", "target": 74})
    assert ("mode", "cool") in t.calls and t.get_cooling_setpoint() == 74
    with pytest.raises(ToolError, match="outside the allowed range"):
        call(main.thermostat, {"target": 95})
    t._mode = "auto"
    with pytest.raises(ToolError, match="Which one"):
        call(main.thermostat, {"target": 72})
    with pytest.raises(ToolError, match="below the cool setpoint"):
        call(main.thermostat, {"heat_setpoint": 78, "cool_setpoint": 74})


def test_not_ready_explains_why(home):
    main.app.state.home = FakeHome([], state="needs_2fa", message="Tell Aurelia the code.")
    with pytest.raises(ToolError, match="two-factor code"):
        call(main.status, {})


def test_heads_up_countdown_can_be_cancelled(home, monkeypatch):
    monkeypatch.setattr(main, "POLICY", {"policy": {"rules": {"heads_up_seconds": 2}}})
    out = call(main.unlock, {}, cancel_after_progress=True)
    assert out == {"cancelled": True, "device": "Front Door"}
    assert home[0].get_locked() is True                                          # nothing happened


def test_describe_never_fails_on_missing_values():
    d = describe(DoorLock(1, None, online=None))
    assert d["kind"] == "lock" and d["name"] is None
