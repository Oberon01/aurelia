"""One-way notes in Kentrell's Trilium (ETAPI). She writes; he reads.

Topics live under a folder note labelled #aureliaLibrary ("Aurelia · Library"),
made on first use. Each topic's note is rewritten whenever the topic grows.

Trilium may sleep behind Lazytainer, which starts it when traffic arrives: the
first request only wakes it. So a request that can't connect (or gets a 502/503/504
from a proxy) is retried for up to wake_seconds. A request Trilium answered, or one
that timed out waiting for an answer, is never retried.
Two-way sync (his edits flowing back) comes later, once governance has earned it.
"""
import asyncio
import logging
import time

import httpx

log = logging.getLogger("function.kepler.trilium")
FOLDER_LABEL = "aureliaLibrary"


class Trilium:
    WAKING = {502, 503, 504}

    def __init__(self, url: str | None, token: str | None, client: httpx.AsyncClient | None = None,
                 wake_seconds: float = 60, retry_every: float = 3):
        self.url, self.token, self._client = (url or "").rstrip("/"), token, client
        self.wake_seconds, self.retry_every = wake_seconds, retry_every
        self._folder: str | None = None

    @property
    def ready(self) -> bool:
        return bool(self.url and self.token)

    async def _send(self, method: str, path: str, headers: dict, **kw) -> httpx.Response:
        if self._client:
            return await self._client.request(method, f"{self.url}/etapi{path}", headers=headers, **kw)
        async with httpx.AsyncClient(timeout=15) as c:
            return await c.request(method, f"{self.url}/etapi{path}", headers=headers, **kw)

    async def _req(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {"Authorization": self.token or "", **kw.pop("headers", {})}
        deadline, woke = time.monotonic() + self.wake_seconds, False
        while True:
            try:
                r = await self._send(method, path, headers, **kw)
                if r.status_code not in self.WAKING:
                    break
                why = f"HTTP {r.status_code}"
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
                if time.monotonic() >= deadline:
                    raise
                why, r = type(exc).__name__, None
            if time.monotonic() >= deadline:
                break
            if not woke:
                log.info("Trilium isn't answering yet (%s); waiting for it to wake", why)
                woke = True
            await asyncio.sleep(self.retry_every)
        r.raise_for_status()
        return r

    async def folder(self, parent: str = "root") -> str:
        if self._folder:
            return self._folder
        found = (await self._req("GET", "/notes", params={"search": f"#{FOLDER_LABEL}", "limit": 1})).json()
        if found.get("results"):
            self._folder = found["results"][0]["noteId"]
            return self._folder
        made = (await self._req("POST", "/create-note", json={
            "parentNoteId": parent, "title": "Aurelia · Library", "type": "text",
            "content": "<p>Kentrell's research library, kept by Aurelia (Kepler). One topic per note.</p>"})).json()
        self._folder = made["note"]["noteId"]
        await self._req("POST", "/attributes", json={"noteId": self._folder, "type": "label", "name": FOLDER_LABEL, "value": ""})
        return self._folder

    async def upsert(self, note_id: str | None, title: str, content: str, parent: str = "root") -> str:
        """Writes the topic's note; returns its id (a new one if it was deleted in Trilium)."""
        if note_id:
            try:
                await self._req("PATCH", f"/notes/{note_id}", json={"title": title})
                await self._req("PUT", f"/notes/{note_id}/content", content=content.encode(),
                                headers={"Content-Type": "text/plain"})
                return note_id
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
        made = (await self._req("POST", "/create-note", json={
            "parentNoteId": await self.folder(parent), "title": title, "type": "text", "content": content})).json()
        return made["note"]["noteId"]

    async def delete(self, note_id: str) -> None:
        try:
            await self._req("DELETE", f"/notes/{note_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
