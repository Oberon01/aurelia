"""Roku TVs, controlled over your home network with Roku's External Control
Protocol (plain HTTP on port 8060; no account, no cloud).

TVs are found by sweeping the local network for port 8060, and are known by
serial number rather than IP address. Every command first checks that the TV
at the remembered address is still the same TV; if it moved (new IP from the
router) or vanished, Orbit sweeps again and follows it. So a static IP is nice
to have, never required.

Why a sweep and not SSDP (Roku's "announce yourself" discovery)? Orbit runs in
a Docker bridge network, which doesn't pass multicast to and from your LAN.
Ordinary connections to LAN addresses work fine, so a sweep does.
"""
import asyncio
import ipaddress
import json
import logging
import re
import time
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree as ET

import httpx

log = logging.getLogger("function.orbit.roku")

PORT = 8060
TVS_KEY = "aurelia:orbit:tvs"
# Tried when no subnets are configured: the ranges common home routers hand out.
COMMON_SUBNETS = ["192.168.0.0/24", "192.168.1.0/24", "192.168.2.0/24", "192.168.4.0/24",
                  "192.168.50.0/24", "192.168.68.0/24", "192.168.86.0/24",
                  "10.0.0.0/24", "10.0.1.0/24", "10.1.10.0/24"]
MAX_HOSTS = 4096               # never sweep more than this many addresses at once
SWEEP_CONCURRENCY = 128

# Words you can use → Roku's key names.
KEYS = {
    "home": "Home", "back": "Back", "select": "Select", "ok": "Select", "enter": "Select",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "play": "Play", "pause": "Play", "resume": "Play", "play_pause": "Play",
    "rewind": "Rev", "fast_forward": "Fwd", "replay": "InstantReplay",
    "info": "Info", "options": "Info", "search": "Search", "backspace": "Backspace",
    "volume_up": "VolumeUp", "volume_down": "VolumeDown", "mute": "VolumeMute",
    "channel_up": "ChannelUp", "channel_down": "ChannelDown",
    "power_on": "PowerOn", "power_off": "PowerOff", "find_remote": "FindRemote",
}
INPUTS = {"hdmi1": "InputHDMI1", "hdmi2": "InputHDMI2", "hdmi3": "InputHDMI3", "hdmi4": "InputHDMI4",
          "av": "InputAV1", "tuner": "InputTuner"}


class RokuError(Exception):
    """A failure to explain in plain words."""


def key_name(word: str) -> str:
    w = re.sub(r"[\s-]+", "_", word.strip().lower())
    if w in KEYS:
        return KEYS[w]
    if w.lower() in {k.lower() for k in KEYS.values()}:              # already a Roku key name
        return next(k for k in KEYS.values() if k.lower() == w.lower())
    raise RokuError(f"I don't know the remote button '{word}'. Buttons: {', '.join(sorted(KEYS))}.")


def input_key(said: str, named: dict[str, str] | None = None) -> str:
    """'hdmi 2', 'HDMI2', 'antenna', or one of your names for an input ('pc')."""
    s = " ".join(re.sub(r"[^\w ]+", " ", said.lower()).split()).removeprefix("the ").removeprefix("my ")
    for yours, real in (named or {}).items():
        if " ".join(yours.lower().split()) == s:
            s = real.lower()
            break
    if m := re.fullmatch(r"hdmi\s*(\d)", s):
        s = f"hdmi{m.group(1)}"
    elif s in ("antenna", "live tv", "cable", "tv tuner"):
        s = "tuner"
    elif s in ("av1", "composite"):
        s = "av"
    if s not in INPUTS:
        yours = f" Your names: {', '.join(sorted(named))}." if named else ""
        raise RokuError(f"I don't know the input '{said}'. Inputs: HDMI 1-4, AV, tuner.{yours}")
    return INPUTS[s]


# Short names → the id suffix Roku TVs use for that input.
INPUT_IDS = {"hdmi1": "hdmi1", "hdmi2": "hdmi2", "hdmi3": "hdmi3", "hdmi4": "hdmi4", "av": "cvbs", "tuner": "dtv"}


def _norm(text: str) -> str:
    s = " ".join(re.sub(r"[^\w ]+", " ", text.lower()).split())
    for lead in ("the ", "my "):
        s = s.removeprefix(lead)
    return s


def pick_input(said: str, tv_inputs: list[dict[str, str]], named: dict[str, str] | None = None) -> dict[str, str]:
    """Choose an input from what the TV reports. Returns {"name", and "id" to launch
    or "key" to press}. Matches the TV's own names first ("PC" if you renamed HDMI 2
    on the TV), then your names from the policy, then standard ones ("hdmi 2")."""
    s = _norm(said)
    for inp in tv_inputs:                                # the TV's names, e.g. "PC", "Live TV"
        n = _norm(inp["name"])
        if s == n or s.replace(" ", "") == n.replace(" ", ""):
            return {"id": inp["id"], "name": inp["name"]}
    for yours, real in (named or {}).items():            # your names in orbit.yaml
        if _norm(yours) == s:
            s = _norm(real)
            break
    try:
        key = input_key(s)
    except RokuError:
        if tv_inputs:
            raise RokuError(f"The TV has no input called '{said}'. Its inputs: "
                            f"{', '.join(i['name'] for i in tv_inputs)}.")
        raise
    suffix = INPUT_IDS[next(k for k, v in INPUTS.items() if v == key)]
    for inp in tv_inputs:
        if inp["id"] == f"tvinput.{suffix}":
            return {"id": inp["id"], "name": inp["name"]}
    if tv_inputs:
        raise RokuError(f"The TV has no input '{said}'. Its inputs: {', '.join(i['name'] for i in tv_inputs)}.")
    return {"key": key, "name": said}                    # TV didn't list inputs: press the button


def parse_info(xml: str) -> dict[str, str]:
    root = ET.fromstring(xml)
    return {child.tag: (child.text or "").strip() for child in root}


class RokuTv:
    """One TV. Shaped like the SmartRent devices so name matching and aliases work the same."""

    def __init__(self, serial: str, ip: str, info: dict[str, str] | None = None):
        self.serial, self.ip, self.info = serial, ip, info or {}
        self.online = True
        self.app: str | None = None
        self.app_id: str | None = None

    @property
    def name(self) -> str:
        i = self.info
        if i.get("user-device-name"):
            return i["user-device-name"]
        if i.get("user-device-location"):
            return f"{i['user-device-location']} TV"
        return i.get("friendly-device-name") or i.get("default-device-name") or i.get("model-name") or "TV"

    @property
    def power(self) -> str | None:
        return self.info.get("power-mode")          # PowerOn, DisplayOff (standby), Ready, Headless

    @property
    def is_on(self) -> bool:
        return self.power == "PowerOn"

    # Same accessors the SmartRent devices have.
    def get_name(self) -> str: return self.name
    def get_online(self) -> bool: return self.online
    def get_battery_powered(self) -> bool: return False
    def get_battery_level(self) -> None: return None

    def to_json(self) -> str:
        return json.dumps({"serial": self.serial, "ip": self.ip, "info": self.info})


class Roku:
    def __init__(self, redis, transport: httpx.AsyncBaseTransport | None = None,
                 probe_timeout: float = 0.5, settle_seconds: float = 2.5):
        self.redis = redis
        self.tvs: dict[str, RokuTv] = {}
        self.last_sweep: float | None = None
        self.probe_timeout, self.settle_seconds = probe_timeout, settle_seconds
        self._transport = transport
        self._sweep_lock = asyncio.Lock()

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=timeout,
                                 limits=httpx.Limits(max_connections=SWEEP_CONCURRENCY))

    # ---- memory ---------------------------------------------------------------------

    async def load(self) -> None:
        for raw in (await self.redis.hgetall(TVS_KEY)).values():
            d = json.loads(raw)
            self.tvs[d["serial"]] = RokuTv(d["serial"], d["ip"], d.get("info"))

    async def _remember(self, tv: RokuTv) -> None:
        self.tvs[tv.serial] = tv
        await self.redis.hset(TVS_KEY, tv.serial, tv.to_json())

    def all(self) -> list[RokuTv]:
        return list(self.tvs.values())

    # ---- discovery -------------------------------------------------------------------

    async def _device_info(self, client: httpx.AsyncClient, ip: str) -> dict[str, str] | None:
        try:
            r = await client.get(f"http://{ip}:{PORT}/query/device-info")
        except (httpx.HTTPError, OSError):
            return None
        if r.status_code == 403:
            raise RokuError("The TV refused the connection. On the TV, set Settings > System > "
                            "Advanced system settings > Control by mobile apps > Network access to Enabled.")
        if not r.is_success:
            return None
        try:
            info = parse_info(r.text)
        except ET.ParseError:
            return None
        return info if info.get("serial-number") else None

    async def sweep(self, subnets: list[str] | None = None) -> list[RokuTv]:
        """Look for Roku devices on the network. Returns the TVs found."""
        async with self._sweep_lock:
            hosts: list[str] = []
            for net in subnets or COMMON_SUBNETS:
                try:
                    hosts += [str(h) for h in ipaddress.ip_network(net, strict=False).hosts()]
                except ValueError:
                    log.warning("ignoring bad subnet %r", net)
                if len(hosts) > MAX_HOSTS:
                    raise RokuError(f"Those subnets cover more than {MAX_HOSTS} addresses; use smaller ones.")
            # Check remembered addresses first: they are usually still right.
            known = [tv.ip for tv in self.tvs.values()]
            hosts = known + [h for h in dict.fromkeys(hosts) if h not in known]

            started = time.monotonic()
            found: list[RokuTv] = []
            refused: list[RokuError] = []
            gate = asyncio.Semaphore(SWEEP_CONCURRENCY)
            async with self._client(self.probe_timeout) as client:
                async def probe(ip: str) -> None:
                    async with gate:
                        try:
                            info = await self._device_info(client, ip)
                        except RokuError as exc:
                            refused.append(exc)
                            return
                    if info and info.get("is-tv", "true") == "true":
                        found.append(RokuTv(info["serial-number"], ip, info))
                await asyncio.gather(*(probe(h) for h in hosts))

            for tv in found:
                old = self.tvs.get(tv.serial)
                if old and old.ip != tv.ip:
                    log.info("%s moved from %s to %s", tv.name, old.ip, tv.ip)
                await self._remember(tv)
            self.last_sweep = time.time()
            log.info("sweep of %d addresses took %.1fs: %s", len(hosts), time.monotonic() - started,
                     ", ".join(f"{t.name} at {t.ip}" for t in found) or "no Roku TVs")
            if not found and refused:
                raise refused[0]
            return found

    # ---- reaching a TV ---------------------------------------------------------------

    async def reach(self, tv: RokuTv, subnets: list[str] | None = None) -> RokuTv:
        """Make sure the TV is where we think it is; follow it if it moved."""
        async with self._client(3) as client:
            info = await self._device_info(client, tv.ip)
        if info and info.get("serial-number") == tv.serial:
            tv.info, tv.online = info, True
            await self._remember(tv)
            return tv
        log.info("%s isn't at %s any more; looking for it", tv.name, tv.ip)
        found = {t.serial: t for t in await self.sweep(subnets)}
        if tv.serial in found:
            return found[tv.serial]
        tv.online = False
        raise RokuError(f"I can't reach {tv.name}. If it's off, turn on Fast TV Start on the TV "
                        "(Settings > System > Power) so it stays on the network while off; "
                        "otherwise check it has power and Wi-Fi.")

    async def _post(self, tv: RokuTv, path: str) -> None:
        async with self._client(5) as client:
            try:
                r = await client.post(f"http://{tv.ip}:{PORT}{path}")
            except httpx.HTTPError as exc:
                raise RokuError(f"{tv.name} didn't respond: {exc}") from exc
        if r.status_code == 403:
            raise RokuError("The TV refused the command. On the TV, set Settings > System > Advanced system "
                            "settings > Control by mobile apps > Network access to Enabled.")
        if not r.is_success:
            raise RokuError(f"{tv.name} rejected {path} ({r.status_code}).")

    async def _get(self, tv: RokuTv, path: str) -> ET.Element:
        async with self._client(5) as client:
            try:
                r = await client.get(f"http://{tv.ip}:{PORT}{path}")
            except httpx.HTTPError as exc:
                raise RokuError(f"{tv.name} didn't respond: {exc}") from exc
        if not r.is_success:
            raise RokuError(f"{tv.name} couldn't answer {path} ({r.status_code}).")
        return ET.fromstring(r.text)

    # ---- commands --------------------------------------------------------------------

    async def keypress(self, tv: RokuTv, key: str) -> None:
        await self._post(tv, f"/keypress/{key}")

    async def type_text(self, tv: RokuTv, text: str) -> None:
        for ch in text:
            await self._post(tv, f"/keypress/Lit_{quote(ch, safe='')}")

    async def _installed(self, tv: RokuTv) -> list[dict[str, str]]:
        root = await self._get(tv, "/query/apps")
        return [{"id": a.get("id"), "type": a.get("type"), "name": (a.text or "").strip()}
                for a in root.findall("app")]

    async def apps(self, tv: RokuTv) -> list[dict[str, str]]:
        """Installed apps (not inputs)."""
        return [a for a in await self._installed(tv) if a["type"] != "tvin"]

    async def inputs(self, tv: RokuTv) -> list[dict[str, str]]:
        """The TV's inputs as the TV names them right now, including names you gave
        them on the TV ("PC" instead of "HDMI 2"). id like 'tvinput.hdmi2'."""
        return [a for a in await self._installed(tv) if a["type"] == "tvin"]

    async def active_app(self, tv: RokuTv) -> str | None:
        root = await self._get(tv, "/query/active-app")
        app = root.find("app")
        showing = app is not None and app.get("id")
        tv.app = (app.text or "").strip() if showing else None
        tv.app_id = app.get("id") if showing else None
        return tv.app                      # None: the home screen

    async def media_state(self, tv: RokuTv) -> str | None:
        """'play', 'pause', 'buffer', 'stop', 'close'; None if the app doesn't say."""
        try:
            root = await self._get(tv, "/query/media-player")
        except (RokuError, ET.ParseError):
            return None
        return root.get("state")

    async def launch(self, tv: RokuTv, app_id: str, content_id: str | None = None,
                     media_type: str | None = None) -> None:
        params = []
        if content_id:
            params.append(f"contentId={quote(content_id, safe='')}")
        if media_type:
            params.append(f"mediaType={quote(media_type, safe='')}")
        await self._post(tv, f"/launch/{app_id}" + (f"?{'&'.join(params)}" if params else ""))

    async def search(self, tv: RokuTv, keyword: str, kind: str | None = None, season: int | None = None,
                     providers: list[str] | None = None, launch: bool = True) -> None:
        """Roku's own search, driven by name (ECP search/browse): with launch, the TV opens the title
        straight in the first of these apps that has it. Roku doesn't promise the launch on every
        model or app, so callers check what the TV shows afterwards."""
        params = [f"keyword={quote(keyword, safe='')}", f"title={quote(keyword, safe='')}", "match-any=true"]
        if kind in ("movie", "tv-show"):
            params.append(f"type={kind}")
        if season:
            params.append(f"season={int(season)}")
        if providers:
            params.append("provider-id=" + ",".join(quote(p, safe="") for p in providers))
        if launch:
            params.append("launch=true")
        await self._post(tv, "/search/browse?" + "&".join(params))

    async def refresh(self, tv: RokuTv) -> RokuTv:
        async with self._client(3) as client:
            info = await self._device_info(client, tv.ip)
        if info:
            tv.info = info
        return tv
