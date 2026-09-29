"""Reaching the web safely: the network guard, fetching, and readable text.

The guard is the important part. Nebula fetches whatever URL it's given, and a
URL can come from a web page or a redirect, not just from Kentrell. So before
every request, including every redirect hop, it checks where the name really
points: nothing on the home network, the server itself, or Aurelia's own
services (the orchestrator, the database) is reachable unless explicitly
allowed in Nebula's policy. Only http and https, and no passwords in URLs.
"""
import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx

USER_AGENT = "Mozilla/5.0 (compatible; Aurelia/1.0; personal assistant)"
MAX_REDIRECTS = 5
Progress = Callable[[int, int | None], Awaitable[None]]      # (bytes so far, total or None)


class WebError(Exception):
    """A failure to explain in plain words."""


@dataclass
class Guard:
    blocked_domains: list[str] = field(default_factory=list)       # never visited
    confirm_domains: list[str] = field(default_factory=list)       # visited only by you, not by Aurelia
    allow_private: list[str] = field(default_factory=list)         # hosts or networks inside the house that are OK
    resolver: Callable[[str, int], Awaitable[list[str]]] | None = None

    @staticmethod
    def _matches(host: str, domains: list[str]) -> bool:
        host = host.lower().rstrip(".")
        return any(host == d.lower().lstrip(".") or host.endswith("." + d.lower().lstrip(".")) for d in domains)

    async def _resolve(self, host: str, port: int) -> list[str]:
        if self.resolver:
            return await self.resolver(host, port)
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return sorted({i[4][0] for i in infos})

    def _allowed_private(self, host: str, ip: ipaddress._BaseAddress) -> bool:
        for entry in self.allow_private:
            try:
                if ip in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                if self._matches(host, [entry]):
                    return True
        return False

    async def check(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise WebError(f"I only open http and https links, not '{parts.scheme or 'none'}'.")
        if parts.username or parts.password:
            raise WebError("That link has a username or password in it; I don't use those.")
        host = parts.hostname
        if not host:
            raise WebError("That doesn't look like a web address.")
        if self._matches(host, self.blocked_domains):
            raise WebError(f"{host} is on Nebula's blocked list.")
        if self._matches(host, self.confirm_domains):
            raise WebError(f"{host} is on your confirm-first list, so I won't visit it myself. Open it yourself.")
        try:
            ips = await self._resolve(host, parts.port or (443 if parts.scheme == "https" else 80))
        except (OSError, UnicodeError):
            raise WebError(f"I couldn't find {host}.")
        for raw in ips:
            ip = ipaddress.ip_address(raw.split("%")[0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                    or ip.is_unspecified or (ip.version == 6 and ip.ipv4_mapped and ip.ipv4_mapped.is_private)):
                if not self._allowed_private(host, ip):
                    raise WebError(f"{host} points inside your network ({ip}); Nebula only goes to the public web.")


@dataclass
class Fetched:
    url: str                      # after redirects
    status: int
    content_type: str
    body: bytes
    truncated: bool
    headers: dict[str, str]


async def fetch(client: httpx.AsyncClient, guard: Guard, url: str, max_bytes: int,
                progress: Progress | None = None, cancelled: Callable[[], bool] | None = None) -> Fetched:
    """GET with the guard checked on every hop. Stops at max_bytes (truncated=True)."""
    for _ in range(MAX_REDIRECTS + 1):
        await guard.check(url)
        try:
            async with client.stream("GET", url, follow_redirects=False,
                                     headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"}) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code >= 400:
                    raise WebError(f"The site answered {r.status_code} ({r.reason_phrase or 'error'}).")
                total = int(r.headers.get("content-length") or 0) or None
                chunks, got, truncated = [], 0, False
                async for chunk in r.aiter_bytes():
                    chunks.append(chunk)
                    got += len(chunk)
                    if progress:
                        await progress(got, total)
                    if got >= max_bytes:
                        truncated = True
                        break
                    if cancelled and cancelled():
                        truncated = True
                        break
                return Fetched(str(r.url), r.status_code, r.headers.get("content-type", ""),
                               b"".join(chunks)[:max_bytes], truncated, dict(r.headers))
        except httpx.TimeoutException:
            raise WebError("The site took too long to answer.")
        except httpx.HTTPError as exc:
            raise WebError(f"I couldn't reach the site: {exc.__class__.__name__}.")
    raise WebError("Too many redirects.")


# ---- readable text ------------------------------------------------------------------------

def _decode(f: Fetched) -> str:
    m = re.search(r"charset=([\w-]+)", f.content_type or "", re.I)
    for enc in ([m.group(1)] if m else []) + ["utf-8", "latin-1"]:
        try:
            return f.body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return f.body.decode("utf-8", "replace")


def readable(f: Fetched, max_chars: int) -> dict[str, Any]:
    """The page as text a person (and the model) can read: title, main text, no menus or ads."""
    ctype = (f.content_type or "").split(";")[0].strip().lower()
    site = urlsplit(f.url).hostname or ""
    if ctype in ("application/pdf",) or f.body[:5] == b"%PDF-":
        return pdf_text(f.body, f.url, site, max_chars, f.truncated)
    raw = _decode(f)
    title = ""
    if "html" in ctype or raw.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        import trafilatura
        text = trafilatura.extract(raw, url=f.url, include_comments=False, include_tables=True,
                                   include_links=False, favor_precision=True) or ""
        meta = trafilatura.extract_metadata(raw, default_url=f.url)
        title = (meta.title if meta and meta.title else "") or _title(raw)
        if not text.strip():
            text = re.sub(r"\s+", " ", re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>|<[^>]+>", " ", raw)).strip()
    elif ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
        text = raw
    else:
        raise WebError(f"That link is a file ({ctype or 'unknown type'}), not a page. I can download it instead.")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    truncated = len(text) > max_chars or f.truncated
    return {"title": title or site, "url": f.url, "site": site, "words": len(text.split()),
            "text": text[:max_chars].rstrip() + ("…" if len(text) > max_chars else ""), "truncated": truncated}


def pdf_text(body: bytes, url: str, site: str, max_chars: int, cut: bool = False) -> dict[str, Any]:
    """A PDF's text, page by page (no OCR: a scanned PDF has no text to give)."""
    import io
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    if cut:
        raise WebError("That PDF is larger than I read in one go; I can download it instead.")
    try:
        reader = PdfReader(io.BytesIO(body))
        pages = [(p.extract_text() or "").strip() for p in reader.pages[:200]]
        title = ((reader.metadata or {}).get("/Title") or "").strip() if reader.metadata else ""
    except (PdfReadError, ValueError, KeyError) as exc:
        raise WebError(f"I couldn't read that PDF ({exc.__class__.__name__}).")
    text = "\n\n".join(p for p in pages if p)
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()
    if not text:
        raise WebError("That PDF is only images (scanned), so there's no text to read.")
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or site
    return {"title": title or name, "url": url, "site": site, "words": len(text.split()), "pages": len(reader.pages),
            "text": text[:max_chars].rstrip() + ("…" if len(text) > max_chars else ""), "truncated": len(text) > max_chars}


def _title(raw: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def filename_for(url: str, headers: dict[str, str]) -> str:
    """From Content-Disposition, else the URL path; made safe for a file system."""
    cd = headers.get("content-disposition", "")
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)|filename=\"?([^\";]+)\"?", cd, re.I)
    name = unquote((m.group(1) or m.group(2)).strip()) if m else unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    name = re.sub(r"[^\w.\- ()]+", "_", name).strip(" ._") or "download"
    return name[:120]


def split_ext(name: str) -> tuple[str, str]:
    """'backup.tar.gz' → ('backup', '.tar.gz'), so a second copy is 'backup (1).tar.gz'."""
    for double in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
        if name.lower().endswith(double):
            return name[: -len(double)], name[-len(double):]
    import os
    return os.path.splitext(name)


def human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


@dataclass
class Saved:
    path: str
    name: str
    bytes: int
    content_type: str
    quarantined: bool
    cancelled: bool


async def download(client: httpx.AsyncClient, guard: Guard, url: str, folder: str, max_bytes: int,
                   blocked_extensions: list[str], progress: Progress | None = None,
                   cancelled: Callable[[], bool] | None = None) -> Saved:
    """Stream a file to disk with the guard on every hop. Files with risky extensions
    go to the quarantine folder; nothing downloaded is ever run or opened here."""
    import os
    for _ in range(MAX_REDIRECTS + 1):
        await guard.check(url)
        try:
            async with client.stream("GET", url, follow_redirects=False, headers={"User-Agent": USER_AGENT}) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code >= 400:
                    raise WebError(f"The site answered {r.status_code} ({r.reason_phrase or 'error'}).")
                total = int(r.headers.get("content-length") or 0) or None
                if total and total > max_bytes:
                    raise WebError(f"That file is {human_size(total)}; the limit is {human_size(max_bytes)}.")
                name = filename_for(str(r.url), dict(r.headers))
                risky = any(name.lower().endswith(ext.lower()) for ext in blocked_extensions)
                target = os.path.join(folder, "quarantine") if risky else folder
                os.makedirs(target, exist_ok=True)
                stem, ext = split_ext(name)
                path, n = os.path.join(target, name), 1
                while os.path.exists(path):
                    path, n = os.path.join(target, f"{stem} ({n}){ext}"), n + 1
                part, got, stopped = path + ".part", 0, False
                try:
                    with open(part, "wb") as fh:
                        async for chunk in r.aiter_bytes(64 * 1024):
                            got += len(chunk)
                            if got > max_bytes:
                                raise WebError(f"That file passed the {human_size(max_bytes)} limit; stopped.")
                            fh.write(chunk)
                            if progress:
                                await progress(got, total)
                            if cancelled and cancelled():
                                stopped = True
                                break
                except BaseException:
                    if os.path.exists(part):
                        os.remove(part)
                    raise
                if stopped:                      # half a file is no use to anyone: don't keep it
                    os.remove(part)
                    return Saved("", name, got, r.headers.get("content-type", ""), risky, True)
                os.replace(part, path)
                return Saved(path, os.path.basename(path), got, r.headers.get("content-type", ""), risky, False)
        except httpx.TimeoutException:
            raise WebError("The site took too long to answer.")
        except httpx.HTTPError as exc:
            raise WebError(f"I couldn't reach the site: {exc.__class__.__name__}.")
    raise WebError("Too many redirects.")
