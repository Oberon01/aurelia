"""A small MCP client: the two transports a connection uses today.

- Streamable HTTP (a server at a URL): each message is a POST; the answer comes back as JSON or as a short
  event stream. The server may hand out a session id, sent back with every later message.
- stdio (a local server: an npm or PyPI package run inside Lagrange's container): newline-delimited
  JSON-RPC over the process's stdin and stdout.

Only what Lagrange needs: initialize, tools/list, tools/call (with progress and cancellation) and ping.
Whatever a server sends back is data. A server's own requests are answered politely and never acted on:
ping, an empty roots list, and "not supported" for anything else (it can't ask me to sample the model or
to ask him things).
"""
import asyncio
import collections
import contextlib
import json
import logging
import os
import re
import signal
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

log = logging.getLogger("lagrange.mcp")

PROTOCOL = "2025-06-18"
CLIENT = {"name": "aurelia-lagrange", "version": "1.0"}
CALL_TIMEOUT = 120.0
LIST_TIMEOUT = 30.0
MAX_MESSAGE = 16 * 1024 * 1024

Progress = Callable[[float, float | None, str | None], Awaitable[None]]


class McpError(Exception):
    pass


class TransportError(McpError):
    """The connection itself failed (unreachable, stopped, too slow, cut off): the session is no good. Whether
    the server acted on a request in flight is unknown, so nothing is ever sent again on its own."""


class NeedsAuth(McpError):
    """401 from the server: it wants a sign-in (or a better token). `metadata` is where its
    protected-resource metadata lives, when it says."""

    def __init__(self, message: str, metadata: str | None = None, scope: str | None = None):
        super().__init__(message)
        self.metadata, self.scope = metadata, scope


def _www_auth(header: str) -> tuple[str | None, str | None]:
    meta = re.search(r'resource_metadata="([^"]+)"', header or "")
    scope = re.search(r'scope="([^"]+)"', header or "")
    return (meta.group(1) if meta else None), (scope.group(1) if scope else None)


def _answer_request(msg: dict) -> dict:
    """A request from the server: answered, never acted on."""
    method = msg.get("method")
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
    if method == "roots/list":
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {"roots": []}}
    return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": f"{method} isn't supported"}}


def _result(msg: dict) -> dict:
    if "error" in msg and msg["error"] is not None:
        e = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
        raise McpError(str(e.get("message") or "the server returned an error")[:500])
    r = msg.get("result")
    return r if isinstance(r, dict) else {}


class Session:
    """What both transports share: ids, the handshake, listing tools, calling one."""

    def __init__(self) -> None:
        self._next = 0
        self.server: dict = {}
        self.version = PROTOCOL
        self.instructions = ""
        self.started = False

    def _id(self) -> int:
        self._next += 1
        return self._next

    async def request(self, method: str, params: dict | None = None, *, timeout: float = LIST_TIMEOUT,
                      progress: Progress | None = None, task: dict | None = None) -> dict:
        raise NotImplementedError

    async def notify(self, method: str, params: dict | None = None) -> None:
        raise NotImplementedError

    async def start(self) -> dict:
        r = await self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT})
        self.version = str(r.get("protocolVersion") or PROTOCOL)
        self.started = True
        self.server = r.get("serverInfo") if isinstance(r.get("serverInfo"), dict) else {}
        self.instructions = str(r.get("instructions") or "")[:1000]
        if "tools" not in (r.get("capabilities") or {}):
            log.info("server %s doesn't say it has tools; listing anyway", self.server.get("name"))
        await self.notify("notifications/initialized")
        return r

    async def list_tools(self) -> list[dict]:
        tools, cursor, pages = [], None, 0
        while pages < 20:
            r = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += [t for t in (r.get("tools") or []) if isinstance(t, dict)]
            cursor, pages = r.get("nextCursor"), pages + 1
            if not cursor:
                break
        return tools

    async def call(self, name: str, args: dict, *, progress: Progress | None = None,
                   timeout: float = CALL_TIMEOUT, task: dict | None = None) -> dict:
        return await self.request("tools/call", {"name": name, "arguments": args or {}}, timeout=timeout,
                                  progress=progress, task=task)

    async def close(self) -> None:
        pass


class HttpSession(Session):
    def __init__(self, url: str, headers: Callable[[], Awaitable[dict]] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        super().__init__()
        self.url = url
        self._headers = headers
        self._sid: str | None = None
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(CALL_TIMEOUT, connect=10), transport=transport,
                                         follow_redirects=False)

    async def _hdrs(self) -> dict:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if self._sid:
            h["Mcp-Session-Id"] = self._sid
        if self.started:
            h["MCP-Protocol-Version"] = self.version
        if self._headers:
            h.update(await self._headers())
        return h

    async def _post(self, msg: dict, *, timeout: float, progress: Progress | None = None,
                    token: str | None = None, retried: bool = False) -> dict | None:
        try:
            async with self._client.stream("POST", self.url, json=msg, headers=await self._hdrs(),
                                           timeout=httpx.Timeout(timeout, connect=10)) as r:
                if r.status_code == 401:
                    meta, scope = _www_auth(r.headers.get("www-authenticate", ""))
                    raise NeedsAuth("The server wants you to sign in.", meta, scope)
                if r.status_code == 404 and self._sid and not retried and msg.get("method") == "initialize":
                    raise TransportError("The server forgot my session.")
                if r.status_code == 404 and self._sid and not retried:
                    # The server forgot our session before reading this message (it never ran it): start again.
                    self._sid = None
                    await self.start()
                    return await self._post(msg, timeout=timeout, progress=progress, token=token, retried=True)
                if r.status_code >= 400:
                    body = (await _capped(r, 2000))[:300].decode(errors="replace")
                    raise McpError(f"The server answered {r.status_code}{': ' + body if body.strip() else ''}")
                sid = r.headers.get("mcp-session-id")
                if sid:
                    self._sid = sid
                if "id" not in msg or r.status_code == 202:
                    return None
                ctype = r.headers.get("content-type", "")
                if "text/event-stream" in ctype:
                    return await self._from_stream(r, msg["id"], progress, token)
                data = json.loads(await _capped(r, MAX_MESSAGE) or b"{}")
                for m in (data if isinstance(data, list) else [data]):
                    if isinstance(m, dict) and m.get("id") == msg["id"] and ("result" in m or "error" in m):
                        return m
                raise McpError("The server's answer didn't match my request.")
        except httpx.TimeoutException:
            raise TransportError("The server took too long to answer.") from None
        except httpx.HTTPError as exc:
            raise TransportError(f"I couldn't reach the server: {exc.__class__.__name__}") from None
        except json.JSONDecodeError:
            raise McpError("The server's answer wasn't JSON.") from None

    async def _from_stream(self, r: httpx.Response, want: int, progress: Progress | None, token: str | None) -> dict:
        data: list[str] = []
        size = 0
        async for line in _lines(r):
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
                size += len(line)
                if size > MAX_MESSAGE:
                    raise McpError("The server's answer was too large.")
                continue
            if line.strip() or not data:
                continue
            raw, data, size = "\n".join(data), [], 0
            try:
                m = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for one in (m if isinstance(m, list) else [m]):
                if not isinstance(one, dict):
                    continue
                if one.get("id") == want and ("result" in one or "error" in one):
                    return one
                if one.get("method") == "notifications/progress" and progress is not None:
                    p = one.get("params") or {}
                    if str(p.get("progressToken")) == str(token):
                        await _report(progress, p)
                elif "method" in one and "id" in one:
                    with contextlib.suppress(McpError):
                        await self._post(_answer_request(one), timeout=10)
        raise TransportError("The server closed its answer before it finished.")

    async def request(self, method, params=None, *, timeout=LIST_TIMEOUT, progress=None, task=None):
        rid = self._id()
        params = dict(params or {})
        token = None
        if progress is not None:
            token = f"p{rid}"
            params["_meta"] = {"progressToken": token}
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        if task is not None:
            task["cancel"] = lambda: self.notify("notifications/cancelled", {"requestId": rid, "reason": "cancelled"})
        try:                                           # a whole answer, however slowly it trickles in
            got = await asyncio.wait_for(self._post(msg, timeout=timeout, progress=progress, token=token), timeout + 5)
        except asyncio.TimeoutError:
            raise TransportError("The server took too long to answer.") from None
        if got is None:
            raise McpError("The server didn't answer that request.")
        return _result(got)

    async def notify(self, method, params=None):
        with contextlib.suppress(McpError):
            await self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}, timeout=10)

    async def close(self):
        if self._sid:
            with contextlib.suppress(Exception):
                await self._client.delete(self.url, headers=await self._hdrs(), timeout=5)
        await self._client.aclose()


async def _capped(r: httpx.Response, limit: int) -> bytes:
    """A response body, refusing more than `limit` bytes (read as it arrives, never all at once)."""
    buf = bytearray()
    async for chunk in r.aiter_bytes():
        buf += chunk
        if len(buf) > limit:
            raise McpError("The server's answer was too large.")
    return bytes(buf)


async def _lines(r: httpx.Response):
    """An event stream's lines, each capped (a line that never ends is refused, not buffered forever)."""
    buf = b""
    async for chunk in r.aiter_bytes():
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line.rstrip(b"\r").decode(errors="replace")
        if len(buf) > MAX_MESSAGE:
            raise McpError("The server's answer was too large.")
    if buf:
        yield buf.rstrip(b"\r").decode(errors="replace")


async def _report(progress: Progress, p: dict) -> None:
    try:
        total = p.get("total")
        await progress(float(p.get("progress") or 0), float(total) if total is not None else None,
                       str(p["message"])[:200] if p.get("message") else None)
    except Exception:
        pass


class StdioSession(Session):
    """A local server: one process, newline-delimited JSON-RPC both ways."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: str | None = None):
        super().__init__()
        self.argv, self.env, self.cwd = argv, env, cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._waiting: dict[int, asyncio.Future] = {}
        self._progress: dict[str, Progress] = {}
        self._reader: asyncio.Task | None = None
        self._errs: asyncio.Task | None = None
        self.stderr: collections.deque[str] = collections.deque(maxlen=40)
        self._write = asyncio.Lock()

    async def start(self) -> dict:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=self.env, cwd=self.cwd, limit=MAX_MESSAGE,
                start_new_session=True)                # its own process group: closing it stops everything it started
        except (FileNotFoundError, PermissionError) as exc:
            raise TransportError(f"I couldn't start the server: {exc}") from None
        self._reader = asyncio.create_task(self._read())
        self._errs = asyncio.create_task(self._read_errs())
        return await super().start()

    def tail(self) -> str:
        return " | ".join(list(self.stderr)[-4:])[:400]

    async def _read_errs(self) -> None:
        assert self._proc and self._proc.stderr
        with contextlib.suppress(Exception):
            async for line in self._proc.stderr:
                t = line.decode(errors="replace").strip()
                if t:
                    self.stderr.append(t[:300])

    async def _read(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            async for line in self._proc.stdout:
                try:
                    m = json.loads(line)
                except json.JSONDecodeError:
                    continue                                   # a server's stray output on stdout
                for one in (m if isinstance(m, list) else [m]):
                    if isinstance(one, dict):
                        await self._dispatch(one)
        except Exception as exc:                               # a line too long, or the pipe broke
            log.warning("stdio reader stopped: %s", exc)
        finally:
            why = TransportError(f"The server stopped{': ' + self.tail() if self.stderr else '.'}")
            for f in self._waiting.values():
                if not f.done():
                    f.set_exception(why)
            self._waiting.clear()

    async def _dispatch(self, m: dict) -> None:
        if "id" in m and ("result" in m or "error" in m):
            f = self._waiting.pop(m["id"], None)
            if f and not f.done():
                f.set_result(m)
        elif m.get("method") == "notifications/progress":
            p = m.get("params") or {}
            cb = self._progress.get(str(p.get("progressToken")))
            if cb:
                await _report(cb, p)
        elif "method" in m and "id" in m:
            await self._send(_answer_request(m))

    async def _send(self, msg: dict) -> None:
        if not self._proc or not self._proc.stdin or self._proc.returncode is not None:
            raise TransportError(f"The server isn't running{': ' + self.tail() if self.stderr else '.'}")
        async with self._write:
            try:
                self._proc.stdin.write((json.dumps(msg) + "\n").encode())
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                raise TransportError(f"The server stopped{': ' + self.tail() if self.stderr else '.'}") from None

    async def request(self, method, params=None, *, timeout=LIST_TIMEOUT, progress=None, task=None):
        rid = self._id()
        params = dict(params or {})
        token = f"p{rid}"
        if progress is not None:
            params["_meta"] = {"progressToken": token}
            self._progress[token] = progress
        fut = asyncio.get_running_loop().create_future()
        self._waiting[rid] = fut
        if task is not None:
            task["cancel"] = lambda: self.notify("notifications/cancelled", {"requestId": rid, "reason": "cancelled"})
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            return _result(await asyncio.wait_for(fut, timeout))
        except asyncio.TimeoutError:
            raise TransportError("The server took too long to answer.") from None
        finally:
            self._waiting.pop(rid, None)
            self._progress.pop(token, None)

    async def notify(self, method, params=None):
        with contextlib.suppress(McpError):
            await self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    @property
    def alive(self) -> bool:
        return bool(self._proc and self._proc.returncode is None)

    async def close(self):
        if self._proc and self._proc.returncode is None:
            with contextlib.suppress(Exception):
                self._proc.stdin.close()
            try:
                await asyncio.wait_for(self._proc.wait(), 3)
            except asyncio.TimeoutError:
                pass
        if self._proc:                                  # npx / uvx start the real server as a child: stop them all
            for sig in (signal.SIGTERM, signal.SIGKILL):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self._proc.pid, sig)
                if sig == signal.SIGTERM:
                    await asyncio.sleep(0.3)
            if self._proc.returncode is None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._proc.wait(), 3)
        for t in (self._reader, self._errs):
            if t:
                t.cancel()
