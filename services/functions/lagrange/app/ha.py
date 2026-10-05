"""The Home Assistant bridge: its REST API for looking and acting, its websocket for what changes.

- states: what Home Assistant reports (every entity, or the ones matching a name, area word or domain).
- call: one of its services ("light.turn_on" on light.kitchen). Checked by Home Assistant's own report of
  the entity afterwards: "done" only when the state it reports is the one asked for, otherwise "asked".
- Changes it reports (locks, lights, switches, doors and windows, leaks, motion, people coming and going)
  go to Pulsar as events, so an automation can wait for them.
"""
import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

log = logging.getLogger("lagrange.ha")

EVENT_DOMAINS = {"lock", "light", "switch", "binary_sensor", "person", "cover", "input_boolean", "fan"}
DEVICE_CLASS_KIND = {"door": "door", "garage_door": "door", "opening": "door", "window": "window",
                     "moisture": "leak_sensor", "motion": "motion_sensor", "occupancy": "motion_sensor",
                     "presence": "motion_sensor"}
# What a service should leave the entity reporting, when there's one clear answer.
EXPECT = {"turn_on": "on", "turn_off": "off", "lock": "locked", "unlock": "unlocked", "open_cover": "open",
          "close_cover": "closed"}
MAX_ENTITIES = 60


class HAError(Exception):
    pass


def kind_of(entity_id: str, attributes: dict | None) -> str:
    domain = entity_id.split(".", 1)[0]
    if domain == "binary_sensor":
        return DEVICE_CLASS_KIND.get(str((attributes or {}).get("device_class") or ""), "sensor")
    return domain


def entity(raw: dict) -> dict:
    a = raw.get("attributes") or {}
    eid = str(raw.get("entity_id") or "")
    return {"id": eid, "name": str(a.get("friendly_name") or eid), "kind": kind_of(eid, a),
            "domain": eid.split(".", 1)[0], "state": str(raw.get("state") or ""),
            "changed": raw.get("last_changed")}


def ws_url(base: str) -> str:
    u = urlsplit(base.rstrip("/"))
    return urlunsplit(("wss" if u.scheme == "https" else "ws", u.netloc, u.path + "/api/websocket", "", ""))


class HomeAssistant:
    def __init__(self, url: str, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self.url = url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.url, timeout=15, transport=transport,
                                         headers={"Authorization": f"Bearer {token}"})
        self.token = token

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str) -> Any:
        try:
            r = await self._client.get(path)
        except httpx.HTTPError as exc:
            raise HAError(f"I couldn't reach Home Assistant: {exc.__class__.__name__}") from None
        if r.status_code == 401:
            raise HAError("Home Assistant didn't accept the token. Make a new long-lived token (your profile, "
                          "Security) and connect again.")
        if r.status_code >= 400:
            raise HAError(f"Home Assistant answered {r.status_code}.")
        return r.json()

    async def check(self) -> str:
        d = await self._get("/api/")
        return str((d or {}).get("message") or "")

    async def states(self) -> list[dict]:
        return [entity(s) for s in await self._get("/api/states") if isinstance(s, dict)]

    async def state(self, entity_id: str) -> dict | None:
        try:
            return entity(await self._get(f"/api/states/{entity_id}"))
        except HAError:
            return None

    async def call(self, domain: str, service: str, entity_id: str | None, data: dict | None,
                   wait: float = 8.0) -> dict:
        body = dict(data or {})
        if entity_id:
            body["entity_id"] = entity_id
        try:
            r = await self._client.post(f"/api/services/{domain}/{service}", json=body)
        except httpx.HTTPError as exc:
            raise HAError(f"I couldn't reach Home Assistant: {exc.__class__.__name__}") from None
        if r.status_code == 400:
            raise HAError(f"Home Assistant refused {domain}.{service}: {r.text[:200]}")
        if r.status_code >= 400:
            raise HAError(f"Home Assistant answered {r.status_code} to {domain}.{service}.")
        want = EXPECT.get(service)
        if service == "toggle" and entity_id:
            before = await self.state(entity_id)
            want = {"on": "off", "off": "on"}.get(before["state"]) if before else None
        if not entity_id:
            return {"done": False, "asked": True, "note": "Asked. There's no single entity to check it against."}
        deadline = time.monotonic() + wait
        now = await self.state(entity_id)
        while want and (not now or now["state"] != want) and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            now = await self.state(entity_id)
        if want and now and now["state"] == want:
            return {"done": True, "entity": now, "note": f"Home Assistant reports {now['name']} {want}."}
        if want:
            return {"done": False, "asked": True, "entity": now,
                    "note": f"Asked, but Home Assistant still reports {now['state'] if now else 'nothing'} for "
                            f"{now['name'] if now else entity_id}."}
        return {"done": False, "asked": True, "entity": now,
                "note": f"Asked. Home Assistant reports {now['state'] if now else 'nothing'} for "
                        f"{now['name'] if now else entity_id}."}


def match(entities: list[dict], query: str | None = None, domain: str | None = None) -> list[dict]:
    words = [w for w in (query or "").lower().split() if w not in ("the", "my", "all", "a")]
    out = []
    for e in entities:
        if domain and e["domain"] != domain and e["kind"] != domain:
            continue
        hay = f"{e['name']} {e['id']} {e['kind']}".lower()
        if all(w in hay for w in words):
            out.append(e)
    return out


async def watch(url: str, token: str, on_change: Callable[[dict, dict | None], Awaitable[None]],
                on_status: Callable[[str], Awaitable[None]] | None = None, *, connect=None) -> None:
    """Follow Home Assistant's state changes until cancelled, reconnecting with backoff."""
    import websockets
    connect = connect or websockets.connect
    backoff = 2.0
    while True:
        try:
            async with connect(ws_url(url), max_size=8 * 1024 * 1024, open_timeout=15) as ws:
                hello = json.loads(await ws.recv())
                if hello.get("type") == "auth_required":
                    await ws.send(json.dumps({"type": "auth", "access_token": token}))
                    ok = json.loads(await ws.recv())
                    if ok.get("type") != "auth_ok":
                        if on_status:
                            await on_status("Home Assistant didn't accept the token.")
                        await asyncio.sleep(300)
                        continue
                await ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))
                backoff = 2.0
                if on_status:
                    await on_status("")
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("type") != "event":
                        continue
                    d = (m.get("event") or {}).get("data") or {}
                    new, old = d.get("new_state"), d.get("old_state")
                    if not isinstance(new, dict):
                        continue
                    eid = str(new.get("entity_id") or "")
                    if eid.split(".", 1)[0] not in EVENT_DOMAINS:
                        continue
                    if isinstance(old, dict) and old.get("state") == new.get("state"):
                        continue                                  # attributes only: nothing happened
                    with contextlib.suppress(Exception):
                        await on_change(entity(new), entity(old) if isinstance(old, dict) else None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Home Assistant websocket: %s", exc)
            if on_status:
                with contextlib.suppress(Exception):
                    await on_status(f"I lost Home Assistant's live updates ({exc.__class__.__name__}); trying again.")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)
