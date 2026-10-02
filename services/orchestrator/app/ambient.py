"""The ambient screen on presentation displays: what it needs from the server.

GET /view/me   who this display is (its role decides whether it's ambient)
GET /ambient   quiet hours and the weather (Open-Meteo, cached; only the coordinates leave the house)
"""
import json
import logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Request

from . import auth

log = logging.getLogger("aurelia.ambient")
router = APIRouter(tags=["ambient"])
WEATHER_KEY = "aurelia:ambient:weather"
WEATHER_SECONDS = 900
CODES = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy", 45: "Fog", 48: "Fog", 51: "Drizzle",
         53: "Drizzle", 55: "Drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle", 61: "Light rain", 63: "Rain",
         65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow",
         77: "Snow grains", 80: "Showers", 81: "Showers", 82: "Heavy showers", 85: "Snow showers",
         86: "Snow showers", 95: "Thunderstorms", 96: "Thunderstorms, hail", 99: "Thunderstorms, hail"}


def in_quiet(spec: str, now: dtime) -> bool:
    """'23:00-07:00' wraps midnight; '' means never quiet."""
    try:
        a, b = (dtime.fromisoformat(x.strip()) for x in spec.split("-"))
    except (ValueError, AttributeError):
        return False
    return (a <= now < b) if a < b else (now >= a or now < b)


async def weather(app) -> dict | None:
    cfg = app.state.config.current.ambient
    if cfg.latitude is None or cfg.longitude is None:
        return None
    redis = app.state.redis
    cached = await redis.get(WEATHER_KEY)
    if cached:
        return json.loads(cached)
    unit = "celsius" if cfg.units == "celsius" else "fahrenheit"
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": cfg.latitude, "longitude": cfg.longitude, "current": "temperature_2m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min", "forecast_days": 1, "temperature_unit": unit,
                "timezone": "auto"})
            r.raise_for_status()
            j = r.json()
        cur, day = j.get("current") or {}, j.get("daily") or {}
        out = {"temp": round(cur["temperature_2m"]), "sky": CODES.get(int(cur.get("weather_code", -1)), ""),
               "high": round(day["temperature_2m_max"][0]), "low": round(day["temperature_2m_min"][0]),
               "unit": "°C" if unit == "celsius" else "°F"}
    except Exception as exc:
        log.info("no weather right now: %s", exc)
        return None
    await redis.set(WEATHER_KEY, json.dumps(out), ex=WEATHER_SECONDS)
    return out


@router.get("/view/me")
async def me(request: Request):
    dev = await auth.asker(request.headers)
    if dev is None:
        return {"device": None, "role": None}
    return {"device": dev["id"], "kind": dev["kind"], "name": dev["name"], "role": dev["role"]}


@router.get("/ambient")
async def ambient(request: Request):
    app = request.app
    cfg = app.state.config.current.ambient
    tz = ZoneInfo(app.state.config.current.digest.timezone)
    now = datetime.now(tz)
    return {"enabled": cfg.enabled, "dim": max(0.1, min(1.0, cfg.dim)),
            "quiet": cfg.enabled and in_quiet(cfg.quiet_hours, now.time()),
            "weather": await weather(app) if cfg.enabled else None}


# ---- what each display shows (send confirms against this; "what's on my screens" reads it) ------

REPORT_SECONDS = 86400


@router.post("/view/screen")
async def report_screen(request: Request):
    """A display says which panels it's showing, whenever that changes (and now and then)."""
    from .constellation import SCREEN_REPORT
    from .events import DISPLAYS
    dev = await auth.asker(request.headers)
    if dev is None or dev["kind"] not in DISPLAYS:
        return {"ok": False}
    try:
        body = await request.json()
    except ValueError:
        return {"ok": False}
    panels = []
    for p in (body.get("panels") or [])[:40]:
        if not isinstance(p, dict):
            continue
        panels.append({"n": p.get("n") if isinstance(p.get("n"), int) else None,
                       "title": str(p.get("title") or "")[:80], "headline": str(p.get("headline") or "")[:160],
                       "tool": str(p.get("tool") or "")[:80], "queued": bool(p.get("queued")),
                       "tasks": [str(t)[:40] for t in (p.get("tasks") or [])[:20]]})
    report = {"panels": panels, "visible": body.get("visible") is not False, "at": int(datetime.now().timestamp())}
    redis = request.app.state.redis
    await redis.set(SCREEN_REPORT.format(dev["id"]), json.dumps(report), ex=REPORT_SECONDS)
    if report["visible"]:                                    # something kept for this display is on it at last
        from .constellation import SENT_KEY
        from .events import PanelEvent, publish
        for t in {t for p in panels for t in p["tasks"]}:
            raw = await redis.getdel(SENT_KEY.format(t))
            if raw:
                s = json.loads(raw)
                await publish(redis, PanelEvent(type="sent.shown", task_id=t, tool="constellation.send",
                                                device=s["sender"], data={"to": s["to"]}))
    return {"ok": True}


@router.post("/view/queued")
async def take_queued(request: Request):
    """What was kept for this display while it wasn't open or on screen (a phone back from a pocket)."""
    from .constellation import DISPLAY_QUEUE
    dev = await auth.asker(request.headers)
    if dev is None:
        return {"events": []}
    q = DISPLAY_QUEUE.format(dev["id"])
    raw = await request.app.state.redis.lrange(q, 0, -1)
    await request.app.state.redis.delete(q)
    out = []
    for r in raw:
        try:
            out.append(json.loads(r))
        except ValueError:
            pass
    return {"events": out}
