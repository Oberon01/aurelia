"""Notifications on his phone (Phase 7): Web Push to the displays that turned them on.

The phone web app is the view installed to the home screen (manifest + service worker). A display
that turns notifications on gives Aurelia a push subscription (its push service's address and the
keys that encrypt what's sent to it). What's sent is encrypted for that one browser (RFC 8291,
aes128gcm), so the push service carries it without being able to read it, and signed with
Aurelia's own key (VAPID, RFC 8292), made once and kept on the server.

What reaches the phone, and only when that display isn't already open and on screen:
  - a new urgent mailbox item (a device asking to pair, a computer that needs attention)
  - something he asked from that display that took a while and is now done (or failed)
  - something sent to that display that's waiting for it
  - a test ("send me a test notification")
Push addresses are accepted only from the real push services (Apple, Google, Mozilla, Microsoft):
nothing makes the server post to anywhere else.
"""
import asyncio
import base64
import json
import logging
import os
import struct
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import auth, db

log = logging.getLogger("aurelia.push")
router = APIRouter(prefix="/push", tags=["push"])

PUSH_HOSTS = ("web.push.apple.com", ".push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com",
              ".notify.windows.com", "android.googleapis.com")
TASK_PUSH_SECONDS = 20          # a request that took at least this long gets a "done" notification
MAILBOX_CURSOR = "aurelia:push:mailbox"
_key_cache: dict[str, Any] = {}


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def allowed_endpoint(endpoint: str) -> bool:
    u = urlsplit(endpoint or "")
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or (h.startswith(".") and host.endswith(h)) for h in PUSH_HOSTS)


# ---- her signing key (VAPID) ---------------------------------------------------------------------

async def vapid_key() -> ec.EllipticCurvePrivateKey:
    if "key" in _key_cache:
        return _key_cache["key"]
    pem = await db.pool().fetchval("SELECT value FROM server_secrets WHERE name='vapid_private'")
    if not pem:
        new = ec.generate_private_key(ec.SECP256R1())
        pem = new.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        await db.pool().execute("INSERT INTO server_secrets (name, value) VALUES ('vapid_private', $1) "
                                "ON CONFLICT (name) DO NOTHING", pem)
        pem = await db.pool().fetchval("SELECT value FROM server_secrets WHERE name='vapid_private'")
    key = serialization.load_pem_private_key(pem.encode(), None)
    _key_cache["key"] = key
    return key


def public_raw(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def usable_contact(c: str) -> bool:
    """A contact Apple accepts: https on a real name, or a mailto address on one (never localhost/.local)."""
    c = (c or "").strip()
    if c.startswith("mailto:"):
        host = c.rsplit("@", 1)[-1].lower() if "@" in c else ""
    elif c.startswith("https://"):
        host = (urlsplit(c).hostname or "").lower()
    else:
        return False
    return "." in host and not host.endswith((".local", ".localhost", ".internal", ".lan")) and host != "localhost" \
        and not host.replace(".", "").isdigit()


async def contact(app) -> str:
    """Who to reach about these pushes: notifications.contact, else the address he opens Aurelia at."""
    cfg = getattr(getattr(app.state.config.current, "notifications", None), "contact", "") if app else ""
    if usable_contact(cfg):
        return cfg.strip()
    seen = await db.pool().fetchval("SELECT value FROM server_secrets WHERE name='public_origin'")
    if usable_contact(seen or ""):
        return seen
    raise ValueError("I don't know Aurelia's public address to sign notifications with (Apple refuses localhost). "
                     "Set notifications.contact in config/aurelia.yaml, or turn notifications on again from your phone.")


def vapid_header(key: ec.EllipticCurvePrivateKey, endpoint: str, now: float | None = None,
                 subject: str = "") -> str:
    u = urlsplit(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int((now or time.time()) + 12 * 3600),
                              "sub": subject}, separators=(",", ":")).encode())
    der = key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={head}.{claims}.{sig}, k={b64u(public_raw(key))}"


# ---- encrypting for one browser (RFC 8291) -----------------------------------------------------------

def _hkdf(salt: bytes, ikm: bytes, info: bytes, n: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=n, salt=salt, info=info).derive(ikm)


def encrypt(payload: bytes, p256dh: str, auth_secret: str, salt: bytes | None = None,
            server_key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    ua_pub = unb64u(p256dh)
    secret = unb64u(auth_secret)
    eph = server_key or ec.generate_private_key(ec.SECP256R1())
    as_pub = public_raw(eph)
    shared = eph.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
    prk = _hkdf(secret, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, prk, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, prk, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)     # one record, the last
    return salt + struct.pack("!IB", 4096, len(as_pub)) + as_pub + body


# ---- sending ---------------------------------------------------------------------------------------

SERVICE_NAMES = {"apple.com": "Apple's push service", "googleapis.com": "Google's push service",
                 "mozilla.com": "Mozilla's push service", "windows.com": "Microsoft's push service"}


def service_name(endpoint: str) -> str:
    host = urlsplit(endpoint).hostname or ""
    return next((v for k, v in SERVICE_NAMES.items() if host.endswith(k)), "the push service")


async def send_one(sub: dict, message: dict, urgency: str = "normal",
                   transport: httpx.AsyncBaseTransport | None = None, subject: str = "") -> tuple[bool, str]:
    """(sent, why not): the push service's own answer when it refuses."""
    if not allowed_endpoint(sub["endpoint"]):
        return False, "not a push service I send to"
    if not usable_contact(subject):
        return False, "no contact address Apple accepts (set notifications.contact)"
    key = await vapid_key()
    data = encrypt(json.dumps(message).encode()[:3000], sub["p256dh"], sub["auth"])
    headers = {"TTL": "86400", "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
               "Urgency": urgency, "Authorization": vapid_header(key, sub["endpoint"], subject=subject)}
    if message.get("tag"):
        headers["Topic"] = "".join(c for c in str(message["tag"]) if c.isalnum() or c in "-_")[:32]
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport) as c:
            r = await c.post(sub["endpoint"], content=data, headers=headers)
    except httpx.HTTPError as exc:
        log.info("push to device %s failed: %s", sub.get("device_id"), exc.__class__.__name__)
        await db.pool().execute("UPDATE push_subscriptions SET failures=failures+1 WHERE id=$1", sub["id"])
        return False, f"I couldn't reach {service_name(sub['endpoint'])} ({exc.__class__.__name__})"
    if r.status_code in (404, 410):                 # that browser unsubscribed or was reset: forget it
        await db.pool().execute("DELETE FROM push_subscriptions WHERE id=$1", sub["id"])
        return False, f"{service_name(sub['endpoint'])} says this device's notifications ended; tap Notify me again"
    if r.status_code >= 400:
        reason = ""
        try:
            reason = str((r.json() or {}).get("reason") or "")
        except ValueError:
            reason = r.text[:80].strip()
        log.info("push to device %s refused by %s: %s %s (contact %s)", sub.get("device_id"),
                 urlsplit(sub["endpoint"]).hostname, r.status_code, reason, subject)
        await db.pool().execute("UPDATE push_subscriptions SET failures=failures+1 WHERE id=$1", sub["id"])
        return False, f"{service_name(sub['endpoint'])} refused it ({r.status_code}{' ' + reason if reason else ''})"
    await db.pool().execute("UPDATE push_subscriptions SET last_ok=now(), failures=0 WHERE id=$1", sub["id"])
    return True, ""


async def on_screen(redis, device_id: int) -> bool:
    """That display is open and visible right now (so he's already looking at it)."""
    from .constellation import DISPLAY_SEEN, SCREEN_REPORT
    if not await redis.exists(DISPLAY_SEEN.format(device_id)):
        return False
    raw = await redis.get(SCREEN_REPORT.format(device_id))
    try:
        rep = json.loads(raw) if raw else None
    except ValueError:
        rep = None
    return bool(rep) and rep.get("visible") is not False and time.time() - float(rep.get("at") or 0) < 180


async def notify(app, title: str, body: str = "", url: str = "/view", tag: str | None = None,
                 devices: list[int] | None = None, urgency: str = "normal", even_on_screen: bool = False,
                 why: list | None = None) -> int:
    """To every subscribed display (or just these), unless it's open and on screen. Returns how many got it;
    why (if given) collects each refusal's reason."""
    q = ("SELECT s.* FROM push_subscriptions s JOIN devices d ON d.id = s.device_id WHERE d.status='approved'"
         + (" AND s.device_id = ANY($1::int[])" if devices is not None else ""))
    rows = await db.pool().fetch(q, devices) if devices is not None else await db.pool().fetch(q)
    sent = 0
    msg = {"title": title[:120], "body": body[:300], "url": url if url.startswith("/") else "/view",
           **({"tag": tag} if tag else {})}
    try:
        subject = await contact(app) if rows else ""
    except ValueError as exc:
        if why is not None:
            why.append(str(exc))
        log.warning("notifications: %s", exc)
        return 0
    for r in rows:
        if not even_on_screen and await on_screen(app.state.redis, r["device_id"]):
            continue
        ok, reason = await send_one(dict(r), msg, urgency, subject=subject)
        if ok:
            sent += 1
        elif why is not None:
            why.append(reason)
    return sent


async def task_done(app, task_id: str, tool: str, status: str, data: dict | None, error: str | None,
                    device: int | None) -> None:
    """Something he asked from this display that took a while: done (or not), when he isn't looking."""
    if device is None:
        return
    try:
        secs = await db.pool().fetchval("SELECT duration_ms FROM task_runs WHERE id=$1", __import__("uuid").UUID(task_id))
        if not secs or secs < TASK_PUSH_SECONDS * 1000:
            return
        if not await db.pool().fetchval("SELECT 1 FROM push_subscriptions WHERE device_id=$1", device):
            return
        head = str((data or {}).get("headline") or "")
        if status == "failed":
            title, body = "That didn't work", (error or head or "")[:200]
        else:
            title, body = "Done", head or tool.split(".")[-1].replace("_", " ").capitalize()
        await notify(app, title, body, "/view", f"task-{task_id[:8]}", devices=[device])
    except Exception:
        log.exception("task-done notification")


async def mailbox_loop(app) -> None:
    """New urgent mailbox items (a device asking to pair, a computer needing attention) reach the phone."""
    redis = app.state.redis
    while True:
        try:
            last = await redis.get(MAILBOX_CURSOR)
            if last is None:                           # first run: nothing old is announced
                top = await db.pool().fetchval("SELECT coalesce(max(id), 0) FROM mailbox_items")
                await redis.set(MAILBOX_CURSOR, top)
            else:
                rows = await db.pool().fetch(
                    "SELECT id, title, summary FROM mailbox_items WHERE id > $1 AND urgent AND status='waiting' "
                    "ORDER BY id LIMIT 10", int(last))
                for r in rows:
                    await notify(app, r["title"], r["summary"] or "", "/view#mailbox", f"mailbox-{r['id']}",
                                 urgency="high")
                    await redis.set(MAILBOX_CURSOR, r["id"])
        except Exception:
            log.exception("mailbox notifications")
        await asyncio.sleep(15)


# ---- the display's side ---------------------------------------------------------------------------

class Subscription(BaseModel):
    endpoint: str
    keys: dict[str, str]


@router.get("/key")
async def public_key():
    return {"key": b64u(public_raw(await vapid_key()))}


@router.post("/subscribe")
async def subscribe(body: Subscription, request: Request):
    dev = await auth.asker(request.headers)
    if dev is None or dev["kind"] not in ("browser",):
        raise HTTPException(403, "Only a paired display can turn notifications on.")
    if not allowed_endpoint(body.endpoint):
        raise HTTPException(400, "That isn't a push service I send to.")
    p256dh, secret = body.keys.get("p256dh", ""), body.keys.get("auth", "")
    try:
        if len(unb64u(p256dh)) != 65 or len(unb64u(secret)) != 16:
            raise ValueError
    except ValueError:
        raise HTTPException(400, "Those notification keys aren't right.")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    origin = f"https://{host.split(',')[0].strip()}"
    if usable_contact(origin):                     # the address he opens Aurelia at: the contact Apple wants
        await db.pool().execute("INSERT INTO server_secrets (name, value) VALUES ('public_origin', $1) "
                                "ON CONFLICT (name) DO UPDATE SET value=$1", origin)
    await db.pool().execute(
        "INSERT INTO push_subscriptions (device_id, endpoint, p256dh, auth) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (endpoint) DO UPDATE SET device_id=$1, p256dh=$3, auth=$4, failures=0",
        dev["id"], body.endpoint, p256dh, secret)
    return {"ok": True, "device": dev["name"]}


@router.post("/unsubscribe")
async def unsubscribe(body: Subscription, request: Request):
    dev = await auth.asker(request.headers)
    if dev is None:
        raise HTTPException(403)
    await db.pool().execute("DELETE FROM push_subscriptions WHERE endpoint=$1 AND device_id=$2", body.endpoint, dev["id"])
    return {"ok": True}


@router.post("/test")
async def test(request: Request):
    """'Send me a test notification': to this display, even while it's open (that's the point)."""
    dev = await auth.asker(request.headers)
    if dev is None:
        raise HTTPException(403)
    why: list = []
    n = await notify(request.app, "Aurelia", "Notifications work on this device.", "/view", "test",
                     devices=[dev["id"]], even_on_screen=True, why=why)
    if not n:
        raise HTTPException(409, "; ".join(why) or "This device hasn't turned notifications on.")
    return {"ok": True, "sent": n}
