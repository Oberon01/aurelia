"""Orbit — Aurelia's smart home function: SmartRent (locks, lights, thermostat,
sensors) and Roku TVs on the home network.

Follows the standard function contract (see Probe): registers with its tools,
returns from /tools/<tool>/invoke at once, reports progress and results, and
honours cancellation. Governance decides each tool's tier (unlock needs your
confirmation); Orbit's own rules (thermostat limits, heads-up delay) come from
its policy file and are enforced here too.
"""
import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pydantic_settings import BaseSettings
from redis.asyncio import Redis

from .aliases import AliasStore
from .devices import DeviceNotFound, describe, is_dimmer, kind_of, resolve
from .home import Home
from .roku import Roku, RokuError, RokuTv, key_name, pick_input

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("function.orbit")


class Settings(BaseSettings):
    orchestrator_url: str = "http://orchestrator:8000"
    function_url: str = "http://orbit:9000"
    redis_url: str = "redis://redis:6379/0"
    smartrent_email: str | None = None
    smartrent_password: str | None = None
    heartbeat_seconds: int = 10
    confirm_after_seconds: float = 3      # re-read a device this long after commanding it


settings = Settings()

NAME = {"type": "string", "description": "Device name or part of it, e.g. 'front door' or 'kitchen'. "
                                         "Optional when there's only one."}
TV = {"type": "string", "description": "Which TV, e.g. 'living room'. Optional when there's only one."}
TOOLS_SPEC = [
    {"name": "status", "description": "Current state of every smart home device: locks, lights, thermostat, sensors, batteries.",
     "input_schema": {"type": "object", "properties": {}}, "progress_unit": "devices", "freshness_seconds": 30},
    {"name": "lock", "description": "Lock a door.",
     "input_schema": {"type": "object", "properties": {"name": NAME}}, "progress_unit": "steps"},
    {"name": "unlock", "description": "Unlock a door.",
     "input_schema": {"type": "object", "properties": {"name": NAME}}, "progress_unit": "steps"},
    {"name": "light", "description": "Turn a light on or off, or set a dimmable light's brightness (0-100).",
     "input_schema": {"type": "object", "properties": {
         "name": NAME, "on": {"type": "boolean"},
         "level": {"type": "integer", "minimum": 0, "maximum": 100}}}, "progress_unit": "steps"},
    {"name": "thermostat", "description": "Change the thermostat: mode (heat, cool, auto, off, aux_heat), "
                                          "target temperature in °F for the current mode, explicit heat/cool setpoints, or fan (on, auto).",
     "input_schema": {"type": "object", "properties": {
         "mode": {"type": "string", "enum": ["heat", "cool", "auto", "off", "aux_heat"]},
         "target": {"type": "integer", "description": "°F, applied to the setpoint for the current mode"},
         "heat_setpoint": {"type": "integer"}, "cool_setpoint": {"type": "integer"},
         "fan_mode": {"type": "string", "enum": ["on", "auto"]}}}, "progress_unit": "steps"},
    {"name": "login", "description": "Finish signing in to SmartRent with the two-factor code the user received.",
     "input_schema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "alias", "description": "Remember what the user calls a device, e.g. 'living room light' means the "
                                     "device SmartRent calls 'Switch 2'. Use when a name didn't match and the user "
                                     "confirms which device they meant.",
     "input_schema": {"type": "object", "properties": {
         "alias": {"type": "string", "description": "What the user calls it"},
         "device": {"type": "string", "description": "The device's name in SmartRent"}},
         "required": ["alias", "device"]}},
    {"name": "aliases", "description": "List the device nicknames the user has saved.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "forget_alias", "description": "Forget a saved device nickname.",
     "input_schema": {"type": "object", "properties": {"alias": {"type": "string"}}, "required": ["alias"]}},
    # ---- TVs (Roku, on the home network) ----
    {"name": "tv_status", "description": "Whether each TV is on, which app or input it's showing, its model.",
     "input_schema": {"type": "object", "properties": {"name": TV}}, "progress_unit": "tvs"},
    {"name": "tv_power", "description": "Turn a TV on or off.",
     "input_schema": {"type": "object", "properties": {"name": TV, "on": {"type": "boolean"}},
                      "required": ["on"]}, "progress_unit": "steps"},
    {"name": "tv_input", "description": "Switch a TV to an input, by the name the TV shows for it (see tv_inputs; "
                                        "e.g. 'PC' if the user renamed HDMI 2) or a standard one: 'hdmi 1'-'hdmi 4', "
                                        "'av', 'tuner'. Turns the TV on if needed.",
     "input_schema": {"type": "object", "properties": {"name": TV, "input": {"type": "string"}},
                      "required": ["input"]}, "progress_unit": "steps"},
    {"name": "tv_app", "description": "Open an app on a TV (YouTube, Netflix, Hulu...). Turns the TV on if needed. "
                                      "With content_id, opens a specific item, e.g. a YouTube video id "
                                      "('dQw4w9WgXcQ') plays that video.",
     "input_schema": {"type": "object", "properties": {
         "name": TV, "app": {"type": "string"},
         "content_id": {"type": "string", "description": "The app's id for a video, show or channel"},
         "media_type": {"type": "string", "description": "Only if the app needs it: movie, episode, series, live"}},
         "required": ["app"]}, "progress_unit": "steps"},
    {"name": "tv_remote", "description": "Press TV remote buttons, in order: home, back, select, up, down, left, right, "
                                         "play, pause, resume, rewind, fast_forward, replay, info, search, backspace, "
                                         "volume_up, volume_down, mute, channel_up, channel_down. 'repeat' presses "
                                         "the buttons that many times (e.g. volume_up 5 times); 'text' types into a "
                                         "search box. You cannot see the TV screen, so only navigate as the user directs.",
     "input_schema": {"type": "object", "properties": {
         "name": TV, "keys": {"type": "array", "items": {"type": "string"}},
         "repeat": {"type": "integer", "minimum": 1, "maximum": 20},
         "text": {"type": "string"}}}, "progress_unit": "presses"},
    {"name": "tv_apps", "description": "List the apps installed on a TV.",
     "input_schema": {"type": "object", "properties": {"name": TV}}},
    {"name": "tv_inputs", "description": "List a TV's inputs (HDMI ports, antenna...) with the names the TV "
                                         "currently gives them, and which one is showing.",
     "input_schema": {"type": "object", "properties": {"name": TV}}},
    {"name": "tv_discover", "description": "Search the home network for Roku TVs again (normally automatic).",
     "input_schema": {"type": "object", "properties": {}}, "progress_unit": "steps"},
]
REGISTRATION = {"name": "orbit", "description": "Smart home: door locks, lights, thermostat and sensors (SmartRent), "
                                         "and TVs (Roku): power, inputs, apps, remote.",
                "url": settings.function_url,
                "tools": [{**t, "cancellable": True} for t in TOOLS_SPEC]}

POLICY: dict = {}          # refreshed from the orchestrator on every heartbeat
_cancelled: set[str] = set()


def rules() -> dict:
    return (POLICY.get("policy") or {}).get("rules") or {}


def tv_rules() -> dict:
    return rules().get("tv") or {}


def subnets() -> list[str] | None:
    return list(tv_rules().get("subnets") or []) or None


async def heartbeat() -> None:
    global POLICY
    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            try:
                await client.post(f"{settings.orchestrator_url}/registry/functions", json=REGISTRATION)
                r = await client.get(f"{settings.orchestrator_url}/governance/functions/orbit")
                if r.is_success:
                    POLICY = r.json()
            except httpx.HTTPError as exc:
                log.warning("registration failed, will retry: %s", exc)
            await asyncio.sleep(settings.heartbeat_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = Redis.from_url(settings.redis_url)
    app.state.aliases = AliasStore(app.state.redis)
    app.state.home = Home(settings.smartrent_email, settings.smartrent_password, app.state.redis)
    app.state.roku = Roku(app.state.redis)
    tasks = [asyncio.create_task(heartbeat()), asyncio.create_task(app.state.home.start()),
             asyncio.create_task(tv_discovery())]
    yield
    for t in tasks:
        t.cancel()
    await app.state.redis.aclose()


app = FastAPI(title="Aurelia function: Orbit", lifespan=lifespan)


async def tv_discovery() -> None:
    """Find TVs at startup, then look again now and then so a new IP is picked up
    before you need it. (Commands also follow a TV that moved, so this is a bonus.)"""
    roku: Roku = app.state.roku
    await roku.load()
    for _ in range(30):                       # wait for the policy, which may name subnets
        if POLICY:
            break
        await asyncio.sleep(0.5)
    while True:
        try:
            await roku.sweep(subnets())
        except Exception as exc:
            log.warning("TV discovery: %s", exc)
        await asyncio.sleep(max(5.0, float(tv_rules().get("rediscover_minutes", 30))) * 60)


# ---- task context --------------------------------------------------------------------

class TaskContext:
    def __init__(self, task_id: str, args: dict, client: httpx.AsyncClient):
        self.task_id, self.args, self._client = task_id, args, client
        self._base = f"{settings.orchestrator_url}/tasks/{task_id}"

    @property
    def cancelled(self) -> bool:
        return self.task_id in _cancelled

    async def progress(self, current: float, total: float | None, unit: str, label: str | None = None) -> None:
        try:
            r = await self._client.post(f"{self._base}/progress", json={
                "current": current, "total": total, "unit": unit, "label": label})
            if r.is_success and r.json().get("cancel"):
                _cancelled.add(self.task_id)
        except httpx.HTTPError:
            pass

    async def heads_up(self, what: str) -> bool:
        """Physical-world heads-up: count down before acting; False if cancelled."""
        seconds = int(rules().get("heads_up_seconds", 0) or 0)
        for left in range(seconds, 0, -1):
            await self.progress(seconds - left, seconds, "seconds", f"{what} in {left}…")
            if self.cancelled:
                return False
            await asyncio.sleep(1)
        return not self.cancelled


class ToolError(Exception):
    """A failure to explain to the user in plain words."""


# ---- tools ----------------------------------------------------------------------------

def home() -> Home:
    return app.state.home


def require_ready() -> list[Any]:
    h = home()
    if h.state != "ready":
        raise ToolError({"needs_2fa": "SmartRent needs a two-factor code before I can do that. " + h.message,
                         "no_credentials": "SmartRent isn't set up yet. " + h.message,
                         "starting": "I'm still connecting to SmartRent; try again in a few seconds.",
                         }.get(h.state, f"SmartRent isn't available: {h.message}"))
    return h.devices()


async def all_aliases() -> dict[str, str]:
    return {**(rules().get("aliases") or {}), **await app.state.aliases.all()}


async def find(kind: str, name: str | None) -> Any:
    devices = require_ready()
    try:
        return resolve(devices, kind, name, aliases=await all_aliases())
    except DeviceNotFound as exc:
        raise ToolError(str(exc))


NO_TV = ("I couldn't find a Roku TV on your network. Check that it's on and connected, and that "
         "Settings > System > Advanced system settings > Control by mobile apps > Network access is Enabled. "
         "If your network uses an unusual address range, add it under tv: subnets in Orbit's policy file.")


async def find_tv(name: str | None) -> RokuTv:
    roku: Roku = app.state.roku
    try:
        if not roku.all():
            await roku.sweep(subnets())
        if not roku.all():
            raise ToolError(NO_TV)
        try:
            tv = resolve(roku.all(), "tv", name, aliases=await all_aliases())
        except DeviceNotFound as exc:
            raise ToolError(str(exc))
        return await roku.reach(tv, subnets())
    except RokuError as exc:
        raise ToolError(str(exc))


async def find_any(name: str) -> Any:
    """A device of any kind by its real name (used when saving an alias)."""
    h = home()
    devices = (h.devices() if h.state == "ready" else []) + app.state.roku.all()
    if not devices:
        require_ready()
    found = []
    for kind in {kind_of(d) for d in devices}:
        try:
            found.append(resolve(devices, kind, name))
        except DeviceNotFound:
            pass
    if len(found) == 1:
        return found[0]
    names = ", ".join(sorted(d.get_name() or "?" for d in devices))
    if not found:
        raise ToolError(f"There's no device called '{name}'. You have: {names}.")
    raise ToolError(f"'{name}' could be {', '.join(d.get_name() for d in found)}. Which one?")


async def confirm_state(device: Any) -> None:
    """Re-read the device from SmartRent so the reported state is real, not assumed."""
    await asyncio.sleep(settings.confirm_after_seconds)
    try:
        await device._async_fetch_state()
    except Exception as exc:
        log.warning("couldn't re-read %s: %s", device.get_name(), exc)


async def status(ctx: TaskContext) -> dict:
    roku: Roku = app.state.roku
    tvs = roku.all()
    ready = home().state == "ready"
    if not ready and not tvs:
        require_ready()                                   # explains what SmartRent needs
    devices = home().devices() if ready else []
    total = len(devices) + len(tvs)
    known = []
    for i, d in enumerate(devices, 1):
        known.append(describe(d))
        await ctx.progress(i, total, "devices")
    for i, tv in enumerate(tvs, len(devices) + 1):
        known.append(await tv_snapshot(tv))
        await ctx.progress(i, total, "devices")
    out: dict[str, Any] = {"devices": known}
    if ready:
        known_ids = {getattr(d, "_device_id", None) for d in devices}
        out["other_devices"] = [{"name": r.get("name"), "kind": r.get("type"),
                                 "readings": {a.get("name"): a.get("state") for a in r.get("attributes", [])}}
                                for r in await home().raw_devices() if r.get("id") not in known_ids]
    else:
        out["smartrent"] = f"Not connected: {home().message}"
    return out


async def _set_lock(ctx: TaskContext, locked: bool) -> dict:
    door = await find("lock", ctx.args.get("name"))
    verb = "Locking" if locked else "Unlocking"
    if not await ctx.heads_up(f"{verb} {door.get_name()}"):
        return {"cancelled": True, "device": door.get_name()}
    await ctx.progress(1, 2, "steps", f"{verb} {door.get_name()}")
    await door.async_set_locked(locked)
    await confirm_state(door)
    await ctx.progress(2, 2, "steps", "Confirmed")
    if door.get_locked() is not locked:
        raise ToolError(f"I sent the command, but {door.get_name()} still reports "
                        f"{'unlocked' if locked else 'locked'}. Check the door.")
    return {"device": door.get_name(), "locked": door.get_locked()}


async def lock(ctx: TaskContext) -> dict:
    return await _set_lock(ctx, True)


async def unlock(ctx: TaskContext) -> dict:
    return await _set_lock(ctx, False)


async def light(ctx: TaskContext) -> dict:
    lamp = await find("light", ctx.args.get("name"))
    level, on = ctx.args.get("level"), ctx.args.get("on")
    if level is None and on is None:
        raise ToolError("Say whether to turn it on or off, or a brightness.")
    if not await ctx.heads_up(f"Switching {lamp.get_name()}"):
        return {"cancelled": True, "device": lamp.get_name()}
    await ctx.progress(1, 2, "steps", f"Switching {lamp.get_name()}")
    if is_dimmer(lamp):
        target = int(level) if level is not None else (99 if on else 0)
        await lamp.async_set_level(max(0, min(99, target)))     # dimmers run 0-99
    else:
        if level is not None and on is None:
            on = int(level) > 0                                  # plain switch: brightness means on/off
        await lamp.async_set_on(bool(on))
    await confirm_state(lamp)
    await ctx.progress(2, 2, "steps", "Confirmed")
    return describe(lamp)


async def thermostat(ctx: TaskContext) -> dict:
    t = await find("thermostat", ctx.args.get("name"))
    a = ctx.args
    limits = rules().get("thermostat", {})
    lo, hi = int(limits.get("min_f", 60)), int(limits.get("max_f", 85))

    def checked(value: Any, label: str) -> int:
        v = int(value)
        if not lo <= v <= hi:
            raise ToolError(f"{v}°F is outside the allowed range for the {label} ({lo}–{hi}°F).")
        return v

    heat = checked(a["heat_setpoint"], "heat setpoint") if a.get("heat_setpoint") is not None else None
    cool = checked(a["cool_setpoint"], "cool setpoint") if a.get("cool_setpoint") is not None else None
    mode = a.get("mode") or t.get_mode()
    if a.get("target") is not None:
        target = checked(a["target"], "target")
        if mode in ("heat", "aux_heat"):
            heat = target
        elif mode == "cool":
            cool = target
        elif mode == "auto":
            raise ToolError("The thermostat is on auto, which has separate heat and cool setpoints. Which one?")
        else:
            raise ToolError("The thermostat is off. Say heat or cool along with the temperature.")
    if heat is not None and cool is not None and heat >= cool:
        raise ToolError("The heat setpoint has to be below the cool setpoint.")
    if not any([a.get("mode"), heat is not None, cool is not None, a.get("fan_mode")]):
        raise ToolError("Say what to change: mode, temperature, or fan.")

    if not await ctx.heads_up(f"Changing {t.get_name()}"):
        return {"cancelled": True, "device": t.get_name()}
    steps = [s for s in [
        ("mode", a.get("mode"), t.async_set_mode),
        ("heat setpoint", heat, t.async_set_heating_setpoint),
        ("cool setpoint", cool, t.async_set_cooling_setpoint),
        ("fan", a.get("fan_mode"), t.async_set_fan_mode),
    ] if s[1] is not None]
    for i, (label, value, setter) in enumerate(steps, 1):
        await ctx.progress(i - 1, len(steps) + 1, "steps", f"Setting {label}")
        await setter(value)
    await confirm_state(t)
    await ctx.progress(len(steps) + 1, len(steps) + 1, "steps", "Confirmed")
    return describe(t)


async def login(ctx: TaskContext) -> dict:
    code = str(ctx.args.get("code", "")).strip()
    if not code:
        raise ToolError("I need the code SmartRent sent you.")
    return {"message": await home().complete_2fa(code)}


async def alias(ctx: TaskContext) -> dict:
    said, device = str(ctx.args.get("alias", "")).strip(), str(ctx.args.get("device", "")).strip()
    if not said or not device:
        raise ToolError("I need both what you call it and which device it is.")
    target = await find_any(device)
    await app.state.aliases.set(said, target.get_name())
    return {"message": f"Got it: '{said}' means {target.get_name()}.", "alias": said, "device": target.get_name()}


async def aliases(ctx: TaskContext) -> dict:
    saved = await app.state.aliases.all()
    return {"aliases": [{"alias": k, "device": v} for k, v in sorted(saved.items())]}


async def forget_alias(ctx: TaskContext) -> dict:
    said = str(ctx.args.get("alias", "")).strip()
    if not await app.state.aliases.forget(said):
        raise ToolError(f"I don't have an alias '{said}'.")
    return {"message": f"Forgot '{said}'."}


# ---- TVs --------------------------------------------------------------------------------

async def tv_snapshot(tv: RokuTv) -> dict:
    """Quick, never-failing read for status lists (no network sweep)."""
    roku: Roku = app.state.roku
    try:
        await roku.refresh(tv)
        await roku.active_app(tv)
        tv.online = True
    except Exception as exc:                          # a status list shouldn't fail over one TV
        log.info("couldn't read %s: %s", tv.name, exc)
        tv.online = False
    return describe(tv)


async def settle() -> None:
    await asyncio.sleep(app.state.roku.settle_seconds)


async def wake(ctx: TaskContext, tv: RokuTv, step: int, total: int) -> None:
    if not tv.is_on:
        await ctx.progress(step, total, "steps", f"Turning on {tv.name}")
        await app.state.roku.keypress(tv, "PowerOn")
        await settle()
        await app.state.roku.refresh(tv)


async def tv_status(ctx: TaskContext) -> dict:
    roku: Roku = app.state.roku
    if ctx.args.get("name"):
        tvs = [await find_tv(ctx.args["name"])]
    else:
        if not roku.all():
            try:
                await roku.sweep(subnets())
            except RokuError as exc:
                raise ToolError(str(exc))
        tvs = roku.all()
        if not tvs:
            raise ToolError(NO_TV)
    out = []
    for i, tv in enumerate(tvs, 1):
        out.append(await tv_snapshot(tv))
        await ctx.progress(i, len(tvs), "tvs")
    return {"tvs": out}


async def tv_power(ctx: TaskContext) -> dict:
    tv = await find_tv(ctx.args.get("name"))
    on = ctx.args.get("on")
    if on is None:
        raise ToolError("Say whether to turn the TV on or off.")
    on = bool(on)
    if tv.is_on == on:
        return {**describe(tv), "message": f"{tv.name} is already {'on' if on else 'off'}."}
    if not await ctx.heads_up(f"Turning {'on' if on else 'off'} {tv.name}"):
        return {"cancelled": True, "device": tv.name}
    await ctx.progress(1, 2, "steps", f"Turning {'on' if on else 'off'} {tv.name}")
    try:
        await app.state.roku.keypress(tv, "PowerOn" if on else "PowerOff")
        await settle()
        await app.state.roku.refresh(tv)
    except RokuError as exc:
        raise ToolError(str(exc))
    await ctx.progress(2, 2, "steps", "Confirmed")
    if tv.is_on != on:
        raise ToolError(f"I sent the command, but {tv.name} still reports {'off' if on else 'on'}.")
    return describe(tv)


async def tv_input(ctx: TaskContext) -> dict:
    tv = await find_tv(ctx.args.get("name"))
    said = str(ctx.args.get("input") or "").strip()
    if not said:
        raise ToolError("Which input?")
    roku: Roku = app.state.roku
    try:
        target = pick_input(said, await roku.inputs(tv), tv_rules().get("inputs"))
        await wake(ctx, tv, 0, 3)
        await ctx.progress(1, 3, "steps", f"Switching to {target['name']}")
        if "id" in target:
            await roku.launch(tv, target["id"])          # the TV's own input entry
        else:
            await roku.keypress(tv, target["key"])
        await settle()
        await roku.active_app(tv)
    except RokuError as exc:
        raise ToolError(str(exc))
    await ctx.progress(3, 3, "steps", "Confirmed")
    if "id" in target and tv.app_id != target["id"]:
        raise ToolError(f"I asked {tv.name} to switch to {target['name']}, but it's showing "
                        f"{tv.app or 'the home screen'}.")
    return {**describe(tv), "input": target["name"]}


async def tv_inputs(ctx: TaskContext) -> dict:
    tv = await find_tv(ctx.args.get("name"))
    roku: Roku = app.state.roku
    try:
        inputs = await roku.inputs(tv)
        await roku.active_app(tv)
    except RokuError as exc:
        raise ToolError(str(exc))
    yours = tv_rules().get("inputs") or {}
    return {"device": tv.name,
            "inputs": [{"name": i["name"], "port": i["id"].removeprefix("tvinput."),
                        "showing": tv.is_on and tv.app_id == i["id"]} for i in inputs],
            **({"your_names": yours} if yours else {})}


def match_app(apps: list[dict[str, str]], said: str) -> dict[str, str]:
    import difflib
    norm = lambda t: re.sub(r"[^a-z0-9]+", "", t.lower())
    want = norm(said.removeprefix("the "))
    by = {norm(a["name"]): a for a in apps if a.get("name")}
    if want in by:
        return by[want]
    starts = [a for n, a in by.items() if n.startswith(want)]
    if len(starts) == 1:
        return starts[0]
    contains = [a for n, a in by.items() if want and want in n]
    if len(contains) == 1:
        return contains[0]
    close = difflib.get_close_matches(want, list(by), n=1, cutoff=0.75)
    if close and not (starts or contains):
        return by[close[0]]
    options = starts or contains
    if options:
        raise ToolError(f"'{said}' could be {', '.join(a['name'] for a in options)}. Which one?")
    raise ToolError(f"There's no app called '{said}' on the TV. Installed: "
                    f"{', '.join(sorted(a['name'] for a in apps))}.")


async def tv_app(ctx: TaskContext) -> dict:
    tv = await find_tv(ctx.args.get("name"))
    said = str(ctx.args.get("app") or "").strip()
    if not said:
        raise ToolError("Which app?")
    try:
        chosen = match_app(await app.state.roku.apps(tv), said)
        await wake(ctx, tv, 0, 3)
        await ctx.progress(1, 3, "steps", f"Opening {chosen['name']}")
        await app.state.roku.launch(tv, chosen["id"], ctx.args.get("content_id"), ctx.args.get("media_type"))
        await settle()
        showing = await app.state.roku.active_app(tv)
    except RokuError as exc:
        raise ToolError(str(exc))
    await ctx.progress(3, 3, "steps", "Confirmed")
    if showing != chosen["name"]:
        raise ToolError(f"I asked {tv.name} to open {chosen['name']}, but it's showing "
                        f"{showing or 'the home screen'}.")
    return {**describe(tv), "app": chosen["name"]}


PLAYBACK = {"pause": "pause", "play": "play", "resume": "play"}     # words with a wanted end state


async def tv_remote(ctx: TaskContext) -> dict:
    tv = await find_tv(ctx.args.get("name"))
    raw = ctx.args.get("keys") or []
    words = [w for w in (raw.split(",") if isinstance(raw, str) else raw) if str(w).strip()]
    text = str(ctx.args.get("text") or "")
    repeat = max(1, min(20, int(ctx.args.get("repeat") or 1)))
    if not words and not text:
        raise ToolError("Say which buttons to press, or what to type.")
    try:
        keys = [(key_name(str(w)), str(w).strip().lower()) for w in words]
    except RokuError as exc:
        raise ToolError(str(exc))
    if not tv.is_on and not all(k in ("PowerOn", "PowerOff") for k, _ in keys):
        raise ToolError(f"{tv.name} is off. Turn it on first.")

    # "pause" / "play" / "resume" on their own: Roku has one play/pause toggle,
    # so check what's happening before pressing it, or "pause" could un-pause.
    if len(keys) == 1 and keys[0][1] in PLAYBACK and repeat == 1 and not text:
        wanted = PLAYBACK[keys[0][1]]
        state = await app.state.roku.media_state(tv)
        if state == wanted:
            return {"device": tv.name, "message": f"Already {'paused' if wanted == 'pause' else 'playing'}."}

    presses = [k for k, _ in keys] * repeat
    total = len(presses) + len(text)
    if total > 80:
        raise ToolError("That's too many presses at once (80 at most).")
    try:
        for i, key in enumerate(presses, 1):
            if ctx.cancelled:
                return {"cancelled": True, "device": tv.name, "pressed": i - 1}
            await app.state.roku.keypress(tv, key)
            await ctx.progress(i, total, "presses", key)
            await asyncio.sleep(0.15)
        if text:
            await app.state.roku.type_text(tv, text)
            await ctx.progress(total, total, "presses", f"Typed {text}")
    except RokuError as exc:
        raise ToolError(str(exc))
    return {"device": tv.name, "pressed": [w for _, w in keys] * repeat, "typed": text or None}


async def tv_apps(ctx: TaskContext) -> dict:
    tv = await find_tv(ctx.args.get("name"))
    try:
        apps = await app.state.roku.apps(tv)
    except RokuError as exc:
        raise ToolError(str(exc))
    return {"device": tv.name, "apps": sorted(a["name"] for a in apps if a.get("name"))}


async def tv_discover(ctx: TaskContext) -> dict:
    await ctx.progress(0, 1, "steps", "Searching the network")
    try:
        found = await app.state.roku.sweep(subnets())
    except RokuError as exc:
        raise ToolError(str(exc))
    await ctx.progress(1, 1, "steps", "Done")
    return {"found": [{"name": t.name, "ip": t.ip, "model": t.info.get("friendly-model-name")} for t in found]}


TOOLS: dict[str, Callable[[TaskContext], Awaitable[dict]]] = {
    "status": status, "lock": lock, "unlock": unlock, "light": light, "thermostat": thermostat, "login": login,
    "alias": alias, "aliases": aliases, "forget_alias": forget_alias,
    "tv_status": tv_status, "tv_power": tv_power, "tv_input": tv_input, "tv_app": tv_app,
    "tv_remote": tv_remote, "tv_apps": tv_apps, "tv_inputs": tv_inputs, "tv_discover": tv_discover,
}


# ---- contract endpoints ----------------------------------------------------------------

class Invoke(BaseModel):
    task_id: str
    args: dict


@app.get("/health")
async def health():
    return {"status": "ok", "smartrent": home().state,
            "tvs": [{"name": t.name, "ip": t.ip} for t in app.state.roku.all()]}


@app.post("/tools/{tool}/invoke", status_code=202)
async def invoke(tool: str, body: Invoke):
    if tool not in TOOLS:
        raise HTTPException(404, f"orbit has no tool '{tool}'")
    asyncio.create_task(run(TOOLS[tool], body.task_id, body.args))
    return {"accepted": body.task_id}


@app.post("/cancel/{task_id}", status_code=202)
async def cancel(task_id: str):
    _cancelled.add(task_id)
    return {"cancelling": task_id}


async def run(tool: Callable[[TaskContext], Awaitable[dict]], task_id: str, args: dict) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        ctx = TaskContext(task_id, args, client)
        try:
            data = await tool(ctx)
            status_ = "cancelled" if data.get("cancelled") else "completed"
            payload = {"status": status_, "partial": status_ == "cancelled", "data": data}
        except ToolError as exc:
            payload = {"status": "failed", "error": str(exc)}
        except Exception as exc:
            log.exception("task %s failed", task_id)
            payload = {"status": "failed", "error": f"Something went wrong: {exc}"}
        finally:
            _cancelled.discard(task_id)
        try:
            await client.post(f"{settings.orchestrator_url}/tasks/{task_id}/result", json=payload)
        except httpx.HTTPError:
            log.error("couldn't report result for %s", task_id)
