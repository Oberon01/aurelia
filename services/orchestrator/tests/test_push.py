"""Notifications on his phone: what's sent is encrypted for that one browser (RFC 8291) and signed
with Aurelia's key (VAPID, RFC 8292), and only real push services are ever posted to."""
import asyncio
import base64
import json
import os
import struct

os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")
os.environ.setdefault("REDIS_URL", "redis://unused:6379/0")

import pytest  # noqa: E402
from cryptography.exceptions import InvalidSignature  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

from app import push  # noqa: E402


def browser():
    """A browser's side: its key pair and auth secret, as a subscription gives them."""
    k = ec.generate_private_key(ec.SECP256R1())
    secret = os.urandom(16)
    return k, push.b64u(push.public_raw(k)), push.b64u(secret), secret


def decrypt(body: bytes, ua_key, secret: bytes) -> bytes:
    """What the browser does with what it receives (RFC 8291, receiver side)."""
    salt, rs, idlen = body[:16], *struct.unpack("!IB", body[16:21])
    as_pub, ct = body[21:21 + idlen], body[21 + idlen:]
    ua_pub = push.public_raw(ua_key)
    shared = ua_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    prk = push._hkdf(secret, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    cek = push._hkdf(salt, prk, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = push._hkdf(salt, prk, b"Content-Encoding: nonce\x00", 12)
    pt = AESGCM(cek).decrypt(nonce, ct, None)
    assert rs == 4096 and pt.endswith(b"\x02")
    return pt[:-1]


def test_only_that_browser_can_read_it():
    k, p256dh, auth, secret = browser()
    body = push.encrypt(b'{"title":"Hi"}', p256dh, auth)
    assert decrypt(body, k, secret) == b'{"title":"Hi"}'
    other, *_ = browser()
    with pytest.raises(Exception):
        decrypt(body, other, secret)


def test_it_is_signed_with_her_key_for_that_push_service():
    key = ec.generate_private_key(ec.SECP256R1())
    h = push.vapid_header(key, "https://web.push.apple.com/QX/abc", now=1_000_000, subject="https://aura.example.ts.net")
    t = h.split("t=")[1].split(",")[0]
    head, claims, sig = t.split(".")
    c = json.loads(push.unb64u(claims))
    assert c["aud"] == "https://web.push.apple.com" and c["exp"] == 1_000_000 + 12 * 3600
    raw = push.unb64u(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key.public_key().verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    with pytest.raises(InvalidSignature):
        ec.generate_private_key(ec.SECP256R1()).public_key().verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    assert h.endswith("k=" + push.b64u(push.public_raw(key)))


@pytest.mark.parametrize("endpoint,ok", [
    ("https://web.push.apple.com/QX", True), ("https://fcm.googleapis.com/fcm/send/x", True),
    ("https://updates.push.services.mozilla.com/wpush/v2/x", True), ("https://db5p.notify.windows.com/w/?token=x", True),
    ("http://fcm.googleapis.com/x", False), ("https://evil.example/web.push.apple.com", False),
    ("https://fcm.googleapis.com.evil.example/x", False), ("https://192.168.1.10:8096/x", False)])
def test_only_real_push_services_are_posted_to(endpoint, ok):
    assert push.allowed_endpoint(endpoint) is ok


def test_a_push_service_that_says_gone_forgets_the_subscription(monkeypatch):
    import httpx
    k, p256dh, auth, secret = browser()
    seen, done = [], []

    class Pool:
        async def execute(self, q, *a): done.append(q.split()[0])
        async def fetchval(self, *a): return None
    monkeypatch.setattr(push.db, "pool", lambda: Pool())
    key = ec.generate_private_key(ec.SECP256R1())

    async def vk(): return key
    monkeypatch.setattr(push, "vapid_key", vk)

    def handler(req):
        seen.append(req)
        return httpx.Response(201 if len(seen) == 1 else 410)
    sub = {"id": 1, "device_id": 2, "endpoint": "https://web.push.apple.com/QX", "p256dh": p256dh, "auth": auth}
    t = httpx.MockTransport(handler)
    assert asyncio.run(push.send_one(sub, {"title": "Done", "body": "Research finished"}, transport=t,
                                    subject="https://aura.example.ts.net")) == (True, "")
    r = seen[0]
    assert r.headers["content-encoding"] == "aes128gcm" and r.headers["authorization"].startswith("vapid t=")
    assert json.loads(decrypt(r.content, k, secret)) == {"title": "Done", "body": "Research finished"}
    ok, why = asyncio.run(push.send_one(sub, {"title": "x"}, transport=t, subject="https://aura.example.ts.net"))
    assert not ok and "notifications ended" in why
    assert done == ["UPDATE", "DELETE"]



@pytest.mark.parametrize("c,ok", [("https://docker-home.tail0150f0.ts.net:8443", True), ("mailto:me@example.com", True),
                                  ("mailto:aurelia@localhost", False), ("https://localhost:8443", False),
                                  ("https://atlas.local", False), ("https://127.0.0.1", False), ("me@example.com", False),
                                  ("http://example.com", False)])
def test_the_contact_is_one_apple_accepts(c, ok):
    assert push.usable_contact(c) is ok


def test_a_refusal_says_the_push_services_own_reason(monkeypatch):
    import httpx
    k, p256dh, auth, secret = browser()

    class Pool:
        async def execute(self, q, *a): pass
    monkeypatch.setattr(push.db, "pool", lambda: Pool())
    key = ec.generate_private_key(ec.SECP256R1())

    async def vk(): return key
    monkeypatch.setattr(push, "vapid_key", vk)
    seen = []
    t = httpx.MockTransport(lambda req: (seen.append(req), httpx.Response(403, json={"reason": "BadJwtToken"}))[1])
    sub = {"id": 1, "device_id": 2, "endpoint": "https://web.push.apple.com/QX", "p256dh": p256dh, "auth": auth}
    ok, why = asyncio.run(push.send_one(sub, {"title": "x"}, transport=t, subject="https://aura.example.ts.net"))
    assert not ok and why == "Apple's push service refused it (403 BadJwtToken)"
    claims = json.loads(push.unb64u(seen[0].headers["authorization"].split("t=")[1].split(".")[1]))
    assert claims["sub"] == "https://aura.example.ts.net"
