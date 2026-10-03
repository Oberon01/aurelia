"""Panel event protocol: orchestrator → display, over Redis pub/sub.

Events go through Redis, not process memory, so any orchestrator replica can
publish and any replica's WebSocket can deliver — required for zero-downtime swaps.
"""
import json
from typing import Any, Literal

from pydantic import BaseModel
from redis.asyncio import Redis

CHANNEL = "aurelia:events"

EventType = Literal["panel.open", "panel.progress", "panel.result", "panel.close",
                    "mailbox.changed",          # the "N for review" chip
                    "confirm.ask",              # a confirmation card that came after her reply (a slow tool)
                    "screen.check",             # "say what you're showing, now" (checking a send)
                    "sent.shown",               # something kept for another display was shown there at last
                    "panels.clear"]             # close panels on this display (all, or data.panels by number)


class PanelEvent(BaseModel):
    type: EventType
    task_id: str
    tool: str
    data: dict[str, Any] = {}
    device: int | None = None      # the display that asked; None = nobody in particular


DISPLAYS = ("browser", "screen")   # the kinds of device that show panels


def for_display(event: dict[str, Any], me: dict[str, Any] | None) -> bool:
    """Whether this display gets this event. Each display keeps its own panels: a panel goes
    only to the display that asked for it (or was sent it). One nobody in particular asked for (a
    schedule, a release, a terminal's request) goes to every display except presentation ones: a
    TV running her shell shows only what's sent to it. Everything else (the mailbox count) goes to all.
    A connection with no device (inside the stack, never through the proxy) gets everything."""
    if me is None:
        return True
    dev = event.get("device")
    if dev is not None:
        return dev == me["id"]
    if str(event.get("type", "")).startswith(("panel.", "confirm.")) and (me.get("kind") == "screen"
                                                         or me.get("role") == "presentation"):
        return False
    return True


async def publish(redis: Redis, event: PanelEvent) -> None:
    await redis.publish(CHANNEL, event.model_dump_json())


def decode(message: bytes | str) -> dict[str, Any]:
    return json.loads(message)
