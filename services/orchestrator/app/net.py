"""One HTTP client for the whole orchestrator, kept open: connections to my functions, Jellyfin, Ollama and
the rest are reused instead of being set up (TLS and all) for every call.

    async with net.session(10) as c:          # the same calls as an httpx.AsyncClient, 10 s timeout
        r = await c.post(url, json=...)

Leaving the block doesn't close anything. A client belongs to the event loop that made it.
"""
import asyncio
from typing import Any

import httpx

_clients: dict[int, tuple[asyncio.AbstractEventLoop, httpx.AsyncClient]] = {}
LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=30)


def client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    got = _clients.get(id(loop))
    if got is None or got[0] is not loop or got[1].is_closed:
        for k, (lp, c) in list(_clients.items()):          # (clients of loops that are gone)
            if lp.is_closed():
                _clients.pop(k, None)
        got = (loop, httpx.AsyncClient(timeout=30, limits=LIMITS))
        _clients[id(loop)] = got
    return got[1]


async def close() -> None:
    loop = asyncio.get_running_loop()
    got = _clients.pop(id(loop), None)
    if got is not None and not got[1].is_closed:
        await got[1].aclose()


class _Scoped:
    """The shared client, with this block's timeout (and redirect choice) as the default for each request."""

    def __init__(self, c: httpx.AsyncClient, timeout: Any, follow_redirects: bool):
        self._c = c
        self._t = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
        self._follow = follow_redirects

    def _kw(self, kw: dict) -> dict:
        kw.setdefault("timeout", self._t)
        return kw

    def build_request(self, method: str, url: Any, **kw) -> httpx.Request:
        return self._c.build_request(method, url, **self._kw(kw))

    async def send(self, request: httpx.Request, **kw) -> httpx.Response:
        kw.setdefault("follow_redirects", self._follow)
        return await self._c.send(request, **kw)

    async def request(self, method: str, url: Any, **kw) -> httpx.Response:
        kw.setdefault("follow_redirects", self._follow)
        return await self._c.request(method, url, **self._kw(kw))

    async def get(self, url: Any, **kw) -> httpx.Response:
        return await self.request("GET", url, **kw)

    async def post(self, url: Any, **kw) -> httpx.Response:
        return await self.request("POST", url, **kw)

    async def put(self, url: Any, **kw) -> httpx.Response:
        return await self.request("PUT", url, **kw)

    async def delete(self, url: Any, **kw) -> httpx.Response:
        return await self.request("DELETE", url, **kw)

    def stream(self, method: str, url: Any, **kw):
        kw.setdefault("follow_redirects", self._follow)
        return self._c.stream(method, url, **self._kw(kw))


class session:
    """`async with session(timeout) as c:`: the shared client for a few calls (nothing to close)."""

    def __init__(self, timeout: Any = 30, follow_redirects: bool = False):
        self.timeout, self.follow = timeout, follow_redirects

    async def __aenter__(self) -> _Scoped:
        return _Scoped(client(), self.timeout, self.follow)

    async def __aexit__(self, *exc) -> bool:
        return False
