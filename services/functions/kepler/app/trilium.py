"""One-way notes in Kentrell's Trilium (ETAPI). She writes; he reads.

Topics live under a folder note labelled #aureliaLibrary ("Aurelia · Library"),
made on first use. Each topic's note is rewritten whenever the topic grows.
Two-way sync (his edits flowing back) comes later, once governance has earned it.
"""
import logging

import httpx

log = logging.getLogger("function.kepler.trilium")
FOLDER_LABEL = "aureliaLibrary"


class Trilium:
    def __init__(self, url: str | None, token: str | None, client: httpx.AsyncClient | None = None):
        self.url, self.token, self._client = (url or "").rstrip("/"), token, client
        self._folder: str | None = None

    @property
    def ready(self) -> bool:
        return bool(self.url and self.token)

    async def _req(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {"Authorization": self.token or "", **kw.pop("headers", {})}
        if self._client:
            r = await self._client.request(method, f"{self.url}/etapi{path}", headers=headers, **kw)
        else:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.request(method, f"{self.url}/etapi{path}", headers=headers, **kw)
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
