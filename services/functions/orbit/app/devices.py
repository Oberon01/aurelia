"""Device descriptions and name matching, independent of SmartRent itself."""
from typing import Any

# Library class name → Aurelia's word for it.
KINDS = {
    "DoorLock": "lock",
    "Thermostat": "thermostat",
    "BinarySwitch": "light",
    "MultilevelSwitch": "light",       # dimmable
    "LeakSensor": "leak_sensor",
    "MotionSensor": "motion_sensor",
}


def kind_of(device: Any) -> str:
    for cls in type(device).__mro__:
        if cls.__name__ in KINDS:
            return KINDS[cls.__name__]
    return "other"


def is_dimmer(device: Any) -> bool:
    return any(c.__name__ == "MultilevelSwitch" for c in type(device).__mro__)


def describe(device: Any) -> dict[str, Any]:
    kind = kind_of(device)
    d: dict[str, Any] = {"name": device.get_name(), "kind": kind, "online": device.get_online()}
    battery = device.get_battery_level() if device.get_battery_powered() else None
    if battery is not None:
        d["battery"] = battery
    if kind == "lock":
        d["locked"] = device.get_locked()
    elif kind == "light":
        if is_dimmer(device):
            level = device.get_level()
            d["level"] = level
            d["on"] = bool(level)
        else:
            d["on"] = device.get_on()
    elif kind == "thermostat":
        d.update(mode=device.get_mode(), current_temp=device.get_current_temp(),
                 heat_setpoint=device.get_heating_setpoint(), cool_setpoint=device.get_cooling_setpoint(),
                 humidity=device.get_current_humidity(), fan_mode=device.get_fan_mode(),
                 operating_state=device.get_operating_state())
    elif kind in ("leak_sensor", "motion_sensor"):
        d["active"] = device.get_active()
    return d


# Words that mean "the <kind>" rather than a particular device's name.
GENERIC_WORDS = {
    "lock": {"door", "doors"},
    "light": {"lights", "lamp", "lamps"},
    "thermostat": {"temperature", "temp", "heat", "ac", "a/c", "air"},
}


class DeviceNotFound(Exception):
    pass


def resolve(devices: list[Any], kind: str, name: str | None) -> Any:
    """Find one device of a kind by (partial, case-insensitive) name.
    With no name, the only device of that kind is used."""
    candidates = [d for d in devices if kind_of(d) == kind]
    if not candidates:
        raise DeviceNotFound(f"There's no {kind.replace('_', ' ')} in your home.")
    generic = GENERIC_WORDS.get(kind, set()) | {kind, kind + "s"}
    if not name or name.lower().strip().removeprefix("the ").strip() in generic:
        if len(candidates) == 1:
            return candidates[0]
        names = ", ".join(sorted(d.get_name() or "?" for d in candidates))
        raise DeviceNotFound(f"Which {kind}? You have: {names}.")
    wanted = name.lower().strip().removeprefix("the ").strip()
    exact = [d for d in candidates if (d.get_name() or "").lower() == wanted]
    if len(exact) == 1:
        return exact[0]
    partial = [d for d in candidates if wanted in (d.get_name() or "").lower()]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(sorted(d.get_name() or "?" for d in candidates))
    if not partial:
        raise DeviceNotFound(f"No {kind} matches '{name}'. You have: {names}.")
    raise DeviceNotFound(f"'{name}' matches more than one {kind}: "
                         f"{', '.join(sorted(d.get_name() for d in partial))}.")
