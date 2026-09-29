"""Tells the display what a release is doing, step by step.

Best effort by design: every update is the release's whole state, sent to the
orchestrator (POST /releases/progress), which shows it as a "New release
detected!" panel. If one update can't get through (say, mid-swap), the next
one carries everything. Nothing here can slow down, block or fail a release.
"""
import logging
from typing import Any

import httpx

log = logging.getLogger("chrysalis.report")


class Reporter:
    def __init__(self, orchestrator_url: str | None):
        self.url = f"{orchestrator_url.rstrip('/')}/releases/progress" if orchestrator_url else None

    async def send(self, state: dict[str, Any]) -> None:
        if not self.url:
            return
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                await client.post(self.url, json=state)
        except Exception as exc:          # never let reporting touch the release
            log.debug("couldn't report release progress: %s", exc)

    def release(self, rid: int, sha: str, author: str, message: str, services: list[str]) -> "ReleaseProgress":
        return ReleaseProgress(self, rid, sha, author, message, services)


class ReleaseProgress:
    """One release's steps. `at(label)` moves to the named step; `watching`
    fills the last step smoothly during the crash-loop watch."""

    def __init__(self, reporter: Reporter, rid: int, sha: str, author: str, message: str, services: list[str]):
        self.reporter = reporter
        self.base = {"release_id": rid, "sha": sha[:8], "author": author,
                     "message": (message or "").strip().splitlines()[0] if message else "", "services": services}
        self.steps: list[str] = []
        self.index = 0

    def plan(self, steps: list[str]) -> None:
        self.steps = steps

    async def at(self, step: str, fraction: float = 0.0, shown: str | None = None) -> None:
        if step in self.steps:
            self.index = self.steps.index(step)
        await self.reporter.send({**self.base, "current": round(self.index + fraction, 3),
                                  "total": len(self.steps), "label": shown or step})

    async def watching(self, elapsed: float, seconds: float, step: str) -> None:
        left = max(0, round(seconds - elapsed))
        await self.at(step, min(0.999, elapsed / seconds) if seconds else 0.0, f"{step} · {left}s left")

    async def finish(self, outcome: str, detail: str) -> None:
        total = len(self.steps) or 1
        await self.reporter.send({**self.base, "current": total if outcome == "live" else self.index,
                                  "total": total, "outcome": outcome, "detail": detail})
