"""Who may talk to Aurelia from outside the stack (Phase 7).

Every device that reaches her through the proxy — a browser, the phone, a
Constellation node, a terminal — has its own key. Traefik asks /auth/check before
passing any request on (forwardAuth); services inside the stack talk to each
other directly and never pass through here.

Getting a key (pairing):
  1. The device asks (/pair/request) and shows a short code, e.g. K7P-4QX.
  2. A mailbox card with the same code appears on his paired devices. Approving
     it — or running `./scripts/pair.sh K7P-4QX` on the server, which is how the
     very first device gets in — lets the device collect its key once.
  3. Browsers get the key as a cookie they can't read; nodes and terminals keep it.
Only a hash of each key is stored. Revoking a device (display or server) takes
effect within KEY_CACHE_SECONDS everywhere, at once here.

What each kind of key may reach (least privilege):
  browser   everything the display does (including mailbox decisions and devices)
  node      only the node API (/nodes/…) — a node can't approve, decide or revoke
  terminal  only /ask (scripts/ask.sh)
"""
import asyncio
import hashlib
import json
import logging
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import db

log = logging.getLogger("aurelia.auth")
router = APIRouter(tags=["auth"])

COOKIE = "aurelia_key"
KINDS = ("browser", "node", "terminal")
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"      # no 0/O, 1/I/L: read aloud or off a TV
PENDING_MINUTES = 15                                    # a request not approved by then expires
MAX_PENDING = 5                                         # at most this many waiting at once
KEY_CACHE_SECONDS = 30
SEEN_EVERY_SECONDS = 60
PUBLIC = {"/pair", "/pair/request", "/pair/status", "/health", "/favicon.ico"}
SCOPES = {"node": ("/nodes/",), "terminal": ("/ask",)}  # prefixes; browser keys aren't limited

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_seen: dict[int, float] = {}


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_code() -> str:
    c = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
    return f"{c[:3]}-{c[3:]}"


def norm_code(code: str) -> str:
    c = re.sub(r"[^A-Z0-9]", "", (code or "").upper())
    return f"{c[:3]}-{c[3:]}" if len(c) == 6 else ""


def new_key() -> str:
    return "aur_" + secrets.token_urlsafe(32)


def allowed(kind: str, path: str) -> bool:
    prefixes = SCOPES.get(kind)
    if prefixes is None:
        return True
    return any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/") for p in prefixes)   # whole segments only


def clean_name(name: Any, fallback: str) -> str:
    n = " ".join(str(name or "").split())[:60]
    return n or fallback


def clean_info(info: Any) -> dict[str, Any]:
    if not isinstance(info, dict):
        return {}
    out = {}
    for k, v in list(info.items())[:20]:
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[str(k)[:40]] = v if not isinstance(v, str) else v[:200]
    return out


# ---- keys ---------------------------------------------------------------------------

async def device_for(key: str) -> dict[str, Any] | None:
    """The approved device holding this key, or None. Cached briefly; revoking clears it here."""
    if not key or not key.startswith("aur_"):
        return None
    h = digest(key)
    hit = _cache.get(h)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    row = await db.pool().fetchrow("SELECT id, kind, name, user_id FROM devices WHERE key_hash=$1 AND status='approved'", h)
    dev = dict(row) if row else None
    if dev:
        _cache[h] = (time.monotonic() + KEY_CACHE_SECONDS, dev)
    else:
        _cache.pop(h, None)
    return dev


async def _touch(dev_id: int) -> None:
    now = time.monotonic()
    if now - _seen.get(dev_id, 0) < SEEN_EVERY_SECONDS:
        return
    _seen[dev_id] = now
    try:
        await db.pool().execute("UPDATE devices SET last_seen=now() WHERE id=$1", dev_id)
    except Exception:
        log.exception("couldn't update last_seen")


def key_from(request: Request) -> str:
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return request.cookies.get(COOKIE, "")


# ---- the entry check (Traefik forwardAuth) ----------------------------------------------

@router.get("/auth/check", include_in_schema=False)
async def check(request: Request):
    uri = request.headers.get("x-forwarded-uri") or "/"
    path = urlsplit(uri).path or "/"
    method = (request.headers.get("x-forwarded-method") or "GET").upper()
    if path in PUBLIC:
        return Response(status_code=200)
    dev = await device_for(key_from(request))
    if dev and allowed(dev["kind"], path):
        asyncio.create_task(_touch(dev["id"]))
        return Response(status_code=200, headers={"X-Aurelia-Device": str(dev["id"]), "X-Aurelia-User": dev["user_id"]})
    if dev:
        return JSONResponse({"detail": "This device's key doesn't reach that."}, status_code=403)
    if method == "GET" and "text/html" in request.headers.get("accept", ""):
        # Not a Location header: Traefik resolves a relative one against this server's own
        # address (http://orchestrator:8000). A page lets the browser resolve it against
        # the address it's actually on (localhost, or the tailnet name).
        to = f"/pair?next={quote(uri, safe='')}"
        return HTMLResponse(f'<!doctype html><meta http-equiv="refresh" content="0;url={to}">'
                            f'<title>Pair with Aurelia</title><a href="{to}">Pair this device</a>', status_code=401,
                            headers={"Cache-Control": "no-store"})
    return JSONResponse({"detail": "This device isn't paired with Aurelia. Open /pair to pair it."}, status_code=401)


# ---- pairing ------------------------------------------------------------------------------

class PairRequest(BaseModel):
    kind: str = "browser"
    name: str = ""
    info: dict[str, Any] = {}


class PairStatus(BaseModel):
    device_id: int
    claim: str


async def expire_old(conn=None) -> None:
    """Requests nobody approved in time expire, and so do their mailbox cards."""
    c = conn or db.pool()
    rows = await c.fetch("UPDATE devices SET status='expired' WHERE status='pending' AND requested_at < now() - "
                         "make_interval(mins => $1) RETURNING mailbox_item", PENDING_MINUTES)
    items = [r["mailbox_item"] for r in rows if r["mailbox_item"]]
    if items:
        await c.execute("UPDATE mailbox_items SET status='expired', updated_at=now() WHERE id = ANY($1::int[]) "
                        "AND status IN ('waiting','deferred')", items)


@router.post("/pair/request")
async def pair_request(body: PairRequest, request: Request):
    if body.kind not in ("browser", "node"):
        raise HTTPException(400, "Kind must be browser or node.")
    pool = db.pool()
    await expire_old()
    if await pool.fetchval("SELECT count(*) FROM devices WHERE status='pending'") >= MAX_PENDING:
        raise HTTPException(429, "Too many devices are waiting to pair. Approve or decline those first.")
    info = clean_info(body.info)
    info.setdefault("from", request.headers.get("x-forwarded-for", "").split(",")[0].strip() or None)
    name = clean_name(body.name, "A browser" if body.kind == "browser" else "A computer")
    claim, code = secrets.token_urlsafe(24), new_code()
    dev_id = await pool.fetchval(
        "INSERT INTO devices (kind, name, pair_code, claim_hash, info) VALUES ($1, $2, $3, $4, $5::jsonb) RETURNING id",
        body.kind, name, code, digest(claim), json.dumps(info))
    await _mailbox_card(request, dev_id, body.kind, name, code, info)
    log.info("pairing requested: device %s (%s, %s)", dev_id, body.kind, name)
    return {"device_id": dev_id, "claim": claim, "code": code, "expires_in": PENDING_MINUTES * 60}


def _describe(kind: str, info: dict[str, Any]) -> str:
    bits = [info.get(k) for k in ("os", "os_version", "hostname", "browser") if info.get(k)]
    what = "A browser" if kind == "browser" else "A computer (Constellation node)"
    return what + (f": {', '.join(str(b) for b in bits)}" if bits else "")


async def _mailbox_card(request: Request, dev_id: int, kind: str, name: str, code: str, info: dict[str, Any]) -> None:
    from . import mailbox
    ev = [_describe(kind, info), f"Asked from {info['from']}" if info.get("from") else "", f"Code on its screen: {code}"]
    item = await db.pool().fetchval(
        "INSERT INTO mailbox_items (kind, fingerprint, source, title, summary, proposal, evidence, strength, confidence, "
        "effect, urgent) VALUES ('device', $1, 'aurelia', $2, $3, $4::jsonb, $5::jsonb, 1, 1, $6, true) RETURNING id",
        f"device:{dev_id}", f"{name} wants to pair",
        f"Approve only if {code} is the code showing on that device right now.",
        json.dumps({"device_id": dev_id, "name": name, "kind": kind, "code": code}),
        json.dumps([e for e in ev if e]),
        "It gets its own key to reach Aurelia" + (" (only the node connection, nothing else)." if kind == "node" else
                                                   ", like this display. You can revoke it any time."))
    await db.pool().execute("UPDATE devices SET mailbox_item=$2 WHERE id=$1", dev_id, item)
    await mailbox.notify(request.app.state.redis)


@router.post("/pair/status")
async def pair_status(body: PairStatus, response: Response):
    """The requester checks in; once approved it collects its key, exactly once."""
    await expire_old()
    row = await db.pool().fetchrow("SELECT * FROM devices WHERE id=$1", body.device_id)
    if row is None or not row["claim_hash"] or not secrets.compare_digest(row["claim_hash"], digest(body.claim)):
        raise HTTPException(404, "No such pairing request.")
    if row["status"] != "approved":
        return {"status": row["status"], "code": row["pair_code"]}
    key = new_key()
    done = await db.pool().fetchval(
        "UPDATE devices SET key_hash=$2, key_issued_at=now(), claim_hash=NULL WHERE id=$1 AND claim_hash IS NOT NULL "
        "AND key_hash IS NULL RETURNING id", row["id"], digest(key))
    if not done:
        raise HTTPException(404, "No such pairing request.")
    log.info("device %s (%s) collected its key", row["id"], row["name"])
    if row["kind"] == "browser":
        set_cookie(response, key)
        return {"status": "approved", "name": row["name"]}
    return {"status": "approved", "name": row["name"], "key": key, "device_id": row["id"]}


def set_cookie(response: Response, key: str) -> None:
    response.set_cookie(COOKIE, key, max_age=365 * 86400, httponly=True, secure=True, samesite="strict", path="/")


@router.get("/pair", include_in_schema=False)
async def pair_page():
    return HTMLResponse((Path(__file__).parent / "static" / "pair.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


# ---- approving, declining, revoking (shared by the mailbox, the display and the server CLI) ----

async def approve(conn, dev_id: int, name: str | None = None) -> str:
    await expire_old(conn)
    row = await conn.fetchrow("SELECT * FROM devices WHERE id=$1 FOR UPDATE", dev_id)
    if row is None:
        raise HTTPException(404, "That device request is gone.")
    if row["status"] == "expired":
        raise HTTPException(409, "That request expired. Ask the device to pair again.")
    if row["status"] != "pending":
        raise HTTPException(409, f"That device is already {row['status']}.")
    await conn.execute("UPDATE devices SET status='approved', approved_at=now(), name=$2 WHERE id=$1",
                       dev_id, clean_name(name, row["name"]))
    log.info("device %s approved", dev_id)
    return f"Approved. {clean_name(name, row['name'])} can reach Aurelia now."


async def decline(conn, dev_id: int) -> None:
    # The claim stays, so the device can learn it wasn't approved; it can never collect a key.
    await conn.execute("UPDATE devices SET status='declined' WHERE id=$1 AND status='pending'", dev_id)


async def revoke(dev_id: int) -> dict[str, Any]:
    row = await db.pool().fetchrow(
        "UPDATE devices SET status='revoked', revoked_at=now() WHERE id=$1 AND status IN "
        "('approved','pending') RETURNING id, name, key_hash", dev_id)
    if row is None:
        raise HTTPException(404, "No such device, or it's already revoked.")
    if row["key_hash"]:
        _cache.pop(row["key_hash"], None)
    log.info("device %s (%s) revoked", row["id"], row["name"])
    return {"id": row["id"], "name": row["name"], "status": "revoked"}


# ---- devices (the display) ------------------------------------------------------------------

def _ago(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    s = int((datetime.now(timezone.utc) - ts).total_seconds())
    return "just now" if s < 90 else f"{s // 60} min ago" if s < 3600 else f"{s // 3600} h ago" if s < 86400 else f"{s // 86400} d ago"


async def listing(current: int | None = None) -> list[dict[str, Any]]:
    await expire_old()
    rows = await db.pool().fetch(
        "SELECT * FROM devices WHERE status IN ('approved','pending') OR revoked_at > now() - interval '7 days' "
        "ORDER BY status='approved' DESC, last_seen DESC NULLS LAST, requested_at DESC")
    out = []
    for r in rows:
        info = r["info"] if isinstance(r["info"], dict) else json.loads(r["info"] or "{}")
        out.append({"id": r["id"], "kind": r["kind"], "name": r["name"], "status": r["status"],
                    "os": " ".join(str(info[k]) for k in ("os", "os_version") if info.get(k)) or None,
                    "browser": info.get("browser"), "hostname": info.get("hostname"),
                    "code": r["pair_code"] if r["status"] == "pending" else None,
                    "last_seen": _ago(r["last_seen"]), "paired": _ago(r["approved_at"]),
                    "this_device": r["id"] == current})
    return out


@router.get("/devices")
async def devices(request: Request):
    cur = request.headers.get("x-aurelia-device")
    return {"devices": await listing(int(cur) if cur and cur.isdigit() else None)}


class Rename(BaseModel):
    name: str


@router.post("/devices/{dev_id}/revoke")
async def revoke_device(dev_id: int):
    return await revoke(dev_id)


@router.post("/devices/{dev_id}/rename")
async def rename_device(dev_id: int, body: Rename):
    name = clean_name(body.name, "")
    if not name:
        raise HTTPException(400, "Give it a name.")
    if not await db.pool().fetchval("UPDATE devices SET name=$2 WHERE id=$1 RETURNING id", dev_id, name):
        raise HTTPException(404, "No such device.")
    return {"id": dev_id, "name": name}


# ---- on the server: python -m app.auth … (scripts/pair.sh) ----------------------------------

async def _cli(argv: list[str]) -> int:
    from .settings import settings
    await db.connect(settings.database_url)
    pool = db.pool()
    cmd = argv[0] if argv else "help"
    try:
        if cmd == "approve" and len(argv) == 2:
            code = norm_code(argv[1])
            await expire_old()
            row = await pool.fetchrow("SELECT id, name, kind, mailbox_item FROM devices WHERE status='pending' AND pair_code=$1",
                                      code)
            if row is None:
                print(f"No device is waiting with code {argv[1]} (requests expire after {PENDING_MINUTES} minutes).")
                return 1
            async with pool.acquire() as conn, conn.transaction():
                msg = await approve(conn, row["id"])
                if row["mailbox_item"]:
                    await conn.execute("UPDATE mailbox_items SET status='approved', decided_at=now(), decided_by='server', "
                                       "applied=proposal, updated_at=now() WHERE id=$1 AND status IN ('waiting','deferred')",
                                       row["mailbox_item"])
            print(msg + " It picks up its key within a few seconds.")
            return 0
        if cmd == "terminal" and len(argv) >= 2:
            key = new_key()
            dev = await pool.fetchval(
                "INSERT INTO devices (kind, name, status, key_hash, key_issued_at, approved_at, info) VALUES "
                "('terminal', $1, 'approved', $2, now(), now(), '{}'::jsonb) RETURNING id", clean_name(" ".join(argv[1:]), "Terminal"), digest(key))
            print(key)
            print(f"(device {dev}: a terminal key, for /ask only)", file=sys.stderr)
            return 0
        if cmd == "revoke" and len(argv) == 2 and argv[1].isdigit():
            print(f"Revoked {(await revoke(int(argv[1])))['name']}.")
            return 0
        if cmd == "list":
            for d in await listing():
                extra = f"code {d['code']}" if d["code"] else f"last seen {d['last_seen'] or 'never'}"
                print(f"{d['id']:>4}  {d['status']:<9} {d['kind']:<9} {d['name']}  ({extra})")
            return 0
        print("usage: approve CODE | list | revoke ID | terminal NAME")
        return 2
    except HTTPException as exc:
        print(exc.detail)
        return 1
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_cli(sys.argv[1:])))
