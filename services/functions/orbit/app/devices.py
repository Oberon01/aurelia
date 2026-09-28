"""Device descriptions and name matching, independent of SmartRent itself."""
import difflib
import re
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


# ---- name matching -----------------------------------------------------------------------
#
# People say "my living room light", "the lamp in the bedroom", "living rm".
# Names are compared after dropping filler words and words that only name the
# kind of device, then matched by: alias → exact → all words present → closest spelling.

FILLER = {"the", "my", "our", "a", "an", "please", "in", "of", "on", "at", "room's"}
KIND_WORDS = {
    "lock": {"lock", "locks", "door", "doors", "deadbolt"},
    "light": {"light", "lights", "lamp", "lamps", "switch", "switches", "bulb", "bulbs", "dimmer"},
    "thermostat": {"thermostat", "temperature", "temp", "heat", "ac", "a/c", "air", "hvac"},
}
ABBREVIATIONS = {"rm": "room", "lr": "living room", "br": "bedroom", "bdrm": "bedroom", "kit": "kitchen"}


def normalize(text: str | None, kind: str) -> str:
    words = re.sub(r"[^\w/ ]+", " ", (text or "").lower()).split()
    words = " ".join(ABBREVIATIONS.get(w, w) for w in words).split()
    return " ".join(w for w in words if w not in FILLER and w not in KIND_WORDS.get(kind, set()))


class DeviceNotFound(Exception):
    pass


def resolve(devices: list[Any], kind: str, name: str | None, aliases: dict[str, str] | None = None) -> Any:
    """Find one device of a kind by name. With no specific name, the only device of
    that kind is used. `aliases` maps what you say to the device's SmartRent name."""
    candidates = [d for d in devices if kind_of(d) == kind]
    if not candidates:
        raise DeviceNotFound(f"There's no {kind.replace('_', ' ')} in your home.")
    names = ", ".join(sorted(d.get_name() or "?" for d in candidates))

    # Aliases first: exactly what you told Aurelia to call it.
    for said, real in (aliases or {}).items():
        if normalize(said, kind) and normalize(said, kind) == normalize(name, kind):
            name = real
            break

    wanted = normalize(name, kind)
    if not wanted:                                   # "the light", "the door", nothing at all
        if len(candidates) == 1:
            return candidates[0]
        raise DeviceNotFound(f"Which {kind}? You have: {names}.")

    by_name = {d: normalize(d.get_name(), kind) for d in candidates}

    exact = [d for d, n in by_name.items() if n == wanted]
    if len(exact) == 1:
        return exact[0]

    words = set(wanted.split())
    containing = [d for d, n in by_name.items() if words <= set(n.split())]
    if len(containing) == 1:
        return containing[0]
    if len(containing) > 1:
        raise DeviceNotFound(f"'{name}' matches more than one {kind}: "
                             f"{', '.join(sorted(d.get_name() for d in containing))}.")

    # Spacing: "livingroom" vs "living room".
    compact = wanted.replace(" ", "")
    joined = [d for d, n in by_name.items() if compact and compact in n.replace(" ", "")]
    if len(joined) == 1:
        return joined[0]

    # Typos: every word you said must closely match its own word in the name
    # ("livng" → "living"), never just resemble the whole name ("living" ≠ "dining").
    def close(word: str, name_words: list[str]) -> bool:
        return bool(difflib.get_close_matches(word, name_words, n=1, cutoff=0.85))

    fuzzy = [d for d, n in by_name.items() if n and all(close(w, n.split()) for w in words)]
    if len(fuzzy) == 1:
        return fuzzy[0]
    raise DeviceNotFound(f"No {kind} matches '{name}'. You have: {names}.")
