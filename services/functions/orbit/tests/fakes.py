"""Stand-ins for smartrent-py devices, named like the real classes."""


class _Device:
    def __init__(self, device_id, name, online=True, battery=None):
        self._device_id, self._name, self._online, self._battery = device_id, name, online, battery
        self.updater_started = False
        self.fetches = 0

    def get_name(self): return self._name
    def get_online(self): return self._online
    def get_battery_powered(self): return self._battery is not None
    def get_battery_level(self): return self._battery
    def start_updater(self): self.updater_started = True
    def set_update_callback(self, fn): self.callbacks = getattr(self, "callbacks", []) + [fn]
    async def _async_fetch_state(self): self.fetches += 1


class DoorLock(_Device):
    def __init__(self, *a, locked=True, jammed=False, **k):
        super().__init__(*a, **k)
        self._locked, self.jammed = locked, jammed

    def get_locked(self): return self._locked

    async def async_set_locked(self, value):
        if not self.jammed:
            self._locked = value


class BinarySwitch(_Device):
    def __init__(self, *a, on=False, **k):
        super().__init__(*a, **k)
        self._on = on

    def get_on(self): return self._on
    async def async_set_on(self, value): self._on = value


class MultilevelSwitch(_Device):
    def __init__(self, *a, level=0, **k):
        super().__init__(*a, **k)
        self._level = level

    def get_level(self): return self._level
    async def async_set_level(self, value): self._level = value


class Thermostat(_Device):
    def __init__(self, *a, mode="heat", **k):
        super().__init__(*a, **k)
        self._mode, self._heat, self._cool, self._fan = mode, 68, 76, "auto"
        self.calls = []

    def get_mode(self): return self._mode
    def get_current_temp(self): return 70
    def get_heating_setpoint(self): return self._heat
    def get_cooling_setpoint(self): return self._cool
    def get_current_humidity(self): return 40
    def get_fan_mode(self): return self._fan
    def get_operating_state(self): return "idle"

    async def async_set_mode(self, v): self.calls.append(("mode", v)); self._mode = v
    async def async_set_heating_setpoint(self, v): self.calls.append(("heat", v)); self._heat = v
    async def async_set_cooling_setpoint(self, v): self.calls.append(("cool", v)); self._cool = v
    async def async_set_fan_mode(self, v): self.calls.append(("fan", v)); self._fan = v


class MotionSensor(_Device):
    def get_active(self): return False


class LeakSensor(_Device):
    def get_active(self): return False


def my_home():
    return [
        DoorLock(1, "Front Door", battery=87),
        BinarySwitch(2, "Living Room Light"),
        MultilevelSwitch(3, "Bedroom Lamp", level=40),
        Thermostat(4, "Thermostat"),
        MotionSensor(5, "Hallway Motion", battery=60),
    ]
