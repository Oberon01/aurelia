"""Panel event protocol: orchestrator → display, over Redis pub/sub.

Events go through Redis, not process memory, so any orchestrator replica can
publish and any replica's WebSocket can deliver — required for zero-downtime swaps.
"""
import json
from typing import Any, Literal

from pydantic import BaseModel
from redis.asyncio import Redis

CHANNEL = "aurelia:events"

EventType = Literal["panel.open", "panel.progress", "panel.result", "panel.close"]


class PanelEvent(BaseModel):
    type: EventType
    task_id: str
    tool: str
    data: dict[str, Any] = {}


async def publish(redis: Redis, event: PanelEvent) -> None:
    await redis.publish(CHANNEL, event.model_dump_json())


def decode(message: bytes | str) -> dict[str, Any]:
    return json.loads(message)
