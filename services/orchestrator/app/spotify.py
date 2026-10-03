"""Spotify for Quasar (step 4): his Spotify, playing on his speakers, checked with Spotify itself.

Aurelia never plays Spotify herself. Spotify plays on Spotify Connect devices: his phone, the Spotify
app on a computer, and his own computers as speakers (librespot, run by the node agent and named after
the computer). Aurelia picks what to play and where through Spotify's Web API, and then reads back
what Spotify says is playing on which device: that, not the request, is what she reports.

Sign-in: Authorization Code with PKCE (no client secret anywhere). He opens /quasar/spotify/login on a
paired display, approves on Spotify's own page, and Spotify sends his browser back to
/quasar/spotify/callback (public: the redirect from accounts.spotify.com carries no Aurelia cookie),
which only accepts a one-time state made by that login a few minutes earlier. The tokens stay on the
server (Redis), are never shown or logged, and refresh themselves.

The client id is public by design (config: quasar.spotify_client_id) and the redirect address is the
one registered on his Spotify developer app (quasar.spotify_redirect_uri).
"""
import base64
import hashlib
import json
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

log = logging.getLogger("aurelia.spotify")

ACCOUNTS = "https://accounts.spotify.com"
API = "https://api.spotify.com/v1"
AUTH_KEY = "aurelia:spotify:auth"
STATE_KEY = "aurelia:spotify:state:{}"
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing "
          "playlist-read-private playlist-read-collaborative user-library-read user-read-recently-played")


class NotSignedIn(ValueError):
    pass


def norm(t: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (t or "").lower()))


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class Spotify:
    def __init__(self, redis, client_id: str, redirect_uri: str, transport: httpx.AsyncBaseTransport | None = None):
        if not client_id or not redirect_uri:
            raise NotSignedIn("Spotify isn't set up: add quasar.spotify_client_id and spotify_redirect_uri to "
                              "config/aurelia.yaml.")
        self.redis, self.client_id, self.redirect = redis, client_id, redirect_uri
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=15, transport=self._transport)

    # ---- signing in ---------------------------------------------------------------------------------

    async def login_url(self) -> str:
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(24)
        await self.redis.set(STATE_KEY.format(state), verifier, ex=600)
        return f"{ACCOUNTS}/authorize?" + urlencode({
            "client_id": self.client_id, "response_type": "code", "redirect_uri": self.redirect,
            "code_challenge_method": "S256", "code_challenge": _challenge(verifier), "state": state,
            "scope": SCOPES})

    async def finish(self, code: str, state: str) -> None:
        verifier = await self.redis.getdel(STATE_KEY.format(state or "-"))
        if not verifier or not code:
            raise ValueError("That sign-in link was already used or expired. Start again from Aurelia.")
        verifier = verifier.decode() if isinstance(verifier, bytes) else verifier
        await self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect,
                           "client_id": self.client_id, "code_verifier": verifier})

    async def _token(self, form: dict) -> dict:
        async with self._client() as c:
            r = await c.post(f"{ACCOUNTS}/api/token", data=form)
        if r.status_code != 200:
            if form.get("grant_type") == "refresh_token" and r.status_code in (400, 401):
                await self.redis.delete(AUTH_KEY)
                raise NotSignedIn("Spotify signed me out. Sign in again: open /quasar/spotify/login on a screen of yours.")
            raise ValueError(f"Spotify didn't accept the sign-in ({r.status_code}).")
        d = r.json()
        old = await self.auth_raw() or {}
        auth = {"access": d["access_token"], "refresh": d.get("refresh_token") or old.get("refresh"),
                "expires": time.time() + int(d.get("expires_in") or 3600) - 60}
        await self.redis.set(AUTH_KEY, json.dumps(auth))
        return auth

    async def auth_raw(self) -> dict | None:
        raw = await self.redis.get(AUTH_KEY)
        return json.loads(raw) if raw else None

    async def signed_in(self) -> bool:
        return bool((await self.auth_raw() or {}).get("refresh"))

    async def access(self) -> str:
        auth = await self.auth_raw()
        if not auth or not auth.get("refresh"):
            raise NotSignedIn("I'm not signed in to your Spotify yet. Open /quasar/spotify/login on one of your "
                              "screens and approve it (once).")
        if auth.get("expires", 0) < time.time():
            auth = await self._token({"grant_type": "refresh_token", "refresh_token": auth["refresh"],
                                      "client_id": self.client_id})
        return auth["access"]

    # ---- the Web API --------------------------------------------------------------------------------

    async def call(self, method: str, path: str, params: dict | None = None, body: Any = None) -> Any:
        for attempt in (0, 1):
            token = await self.access()
            async with self._client() as c:
                try:
                    r = await c.request(method, f"{API}{path}", params=params, json=body,
                                        headers={"Authorization": f"Bearer {token}"})
                except httpx.HTTPError as exc:
                    raise ValueError(f"I can't reach Spotify ({exc.__class__.__name__}).")
            if r.status_code == 401 and attempt == 0:
                auth = await self.auth_raw() or {}
                auth["expires"] = 0                          # refresh and try once more
                await self.redis.set(AUTH_KEY, json.dumps(auth))
                continue
            if r.status_code == 404 and "/me/player" in path:
                raise ValueError("Spotify says there's no active device to do that on.")
            if r.status_code == 403 and "/me/player" in path:
                raise ValueError("Spotify refused: controlling playback needs Spotify Premium on this account.")
            if r.status_code == 429:
                raise ValueError("Spotify asked me to slow down; try again in a moment.")
            if r.status_code >= 400:
                try:
                    msg = r.json().get("error", {}).get("message")
                except ValueError:
                    msg = None
                raise ValueError(f"Spotify said no ({r.status_code}{': ' + msg if msg else ''}).")
            return r.json() if r.content and "json" in r.headers.get("content-type", "") else None
        return None

    async def devices(self) -> list[dict]:
        return (await self.call("GET", "/me/player/devices") or {}).get("devices") or []

    async def state(self) -> dict:
        return await self.call("GET", "/me/player", {"additional_types": "episode"}) or {}

    async def search(self, q: str, types: str = "track,album,artist,playlist", limit: int = 5) -> dict:
        return await self.call("GET", "/search", {"q": q, "type": types, "limit": limit}) or {}

    async def my_playlists(self) -> list[dict]:
        return (await self.call("GET", "/me/playlists", {"limit": 50}) or {}).get("items") or []


# ---- choosing what and where ---------------------------------------------------------------------

KINDS = {"song": "track", "track": "track", "album": "album", "artist": "artist", "playlist": "playlist",
         "band": "artist", "record": "album"}


def pick(found: dict, asked: str, kind: str | None, mine: list[dict]) -> dict | None:
    """What he meant: one of his own playlists by name first; then an exact name of the kind he said
    (or any kind: artist, album, playlist, track); else Spotify's own top hit."""
    a = norm(asked)
    if kind in (None, "playlist"):
        for p in mine:
            if p and norm(p.get("name")) == a:
                return {**p, "type": "playlist"}
    order = [kind] if kind else ["artist", "album", "playlist", "track"]
    for k in order:
        for it in ((found.get(f"{k}s") or {}).get("items") or []):
            if it and norm(it.get("name")) == a:
                return {**it, "type": k}
    for k in (order if kind else ["track", "artist", "album", "playlist"]):
        items = [i for i in ((found.get(f"{k}s") or {}).get("items") or []) if i]
        if items:
            return {**items[0], "type": k}
    return None


def label(it: dict) -> str:
    if it.get("type") == "track":
        who = ", ".join(a.get("name", "") for a in it.get("artists") or [])
        return f"{it.get('name')} by {who}" if who else it.get("name") or ""
    if it.get("type") == "album":
        who = ", ".join(a.get("name", "") for a in it.get("artists") or [])
        return f"the album {it.get('name')}" + (f" by {who}" if who else "")
    if it.get("type") == "playlist":
        return f"the playlist {it.get('name')}"
    return it.get("name") or ""


def device_for(devices: list[dict], on: str) -> dict | None:
    """A Spotify Connect device by what he calls it (its name; 'the TV' matches 'Living Room TV')."""
    want = norm(re.sub(r"^(?:the|my)\s+", "", on or "", flags=re.I))
    if not want:
        active = [d for d in devices if d.get("is_active")]
        return active[0] if active else None
    exact = [d for d in devices if norm(d.get("name")) == want]
    if exact:
        return exact[0]
    words = set(want.split())
    part = [d for d in devices if words <= set(norm(d.get("name")).split())]
    return part[0] if len(part) == 1 else None


def playing_now(st: dict, device_id: str | None = None) -> dict:
    """What Spotify itself says: for the panel (the same shape as my player's)."""
    item = st.get("item") or {}
    dev = st.get("device") or {}
    art = ((item.get("album") or {}).get("images") or [{}])[0].get("url") if item else None
    out = {"node": dev.get("name") or "", "active": bool(st.get("is_playing")) or bool(item), "kind": "audio",
           "title": label({**item, "type": "track"}) if item.get("type") == "track" else item.get("name") or "",
           "position": round((st.get("progress_ms") or 0) / 1000, 1),
           "duration": round((item.get("duration_ms") or 0) / 1000, 1) or None,
           "paused": not st.get("is_playing"), "volume": dev.get("volume_percent"), "source": "spotify"}
    if art:
        out["art"] = art
    return {k: v for k, v in out.items() if v is not None}
