"""The SmartRent connection: login (including 2FA through Aurelia), saved tokens,
live device updates.

smartrent-py keeps its tokens only in memory and asks for 2FA codes with
input(), which would hang a container. This module:
  - saves the refresh token in Redis after every login or refresh, and restores
    it on start, so restarts (every deploy) reuse the session;
  - replaces the library's input() so a needed 2FA code becomes a clear state
    ("needs_2fa") that you answer with orbit.login.
"""
import asyncio
import json
import logging
from typing import Any

log = logging.getLogger("orbit.home")

TOKENS_KEY = "aurelia:orbit:smartrent_tokens"


class Needs2FA(Exception):
    pass


def _no_prompt(*_args, **_kwargs):
    raise Needs2FA("SmartRent wants a two-factor code")


class Home:
    def __init__(self, email: str | None, password: str | None, redis, api_factory=None, on_change=None):
        self.email, self.password, self.redis = email, password, redis
        self._api_factory = api_factory
        self._on_change = on_change          # a device's live change -> Pulsar (an event an automation can wait for)
        self.api = None
        self.state = "starting"          # starting | no_credentials | needs_2fa | ready | error
        self.message = ""
        self._pending_tfa: str | None = None
        self._lock = asyncio.Lock()

    # ---- connection ----------------------------------------------------------------

    def _make_api(self):
        if self._api_factory:
            return self._api_factory(self.email, self.password)
        import smartrent.utils
        from smartrent.api import API
        smartrent.utils.input = _no_prompt        # never block on a terminal prompt
        return API(self.email, self.password)

    async def start(self) -> None:
        async with self._lock:
            if not self.email or not self.password:
                self._set("no_credentials", "Add SMARTRENT_EMAIL and SMARTRENT_PASSWORD to .env on the server.")
                return
            self.api = self._make_api()
            self._wrap_refresh()
            if await self._restore_tokens():
                try:
                    await self._load_devices()
                    return
                except Exception as exc:          # saved session no longer valid
                    log.warning("saved SmartRent session didn't work (%s); logging in again", exc)
                    self.api.client._token = self.api.client._refresh_token = self.api.client._token_exp_time = None
            await self._login_with_password()

    async def _login_with_password(self) -> None:
        client = self.api.client
        try:
            resp = await client._async_refresh_tokens_via_email()
        except Exception as exc:
            self._set("error", f"Couldn't reach SmartRent: {exc}")
            return
        if resp.get("tfa_api_token"):
            self._pending_tfa = resp["tfa_api_token"]
            self._set("needs_2fa", "SmartRent sent you a two-factor code. Tell Aurelia: "
                                   "\"SmartRent code 123456\" (your code).")
            return
        await self._accept_tokens(resp)

    async def complete_2fa(self, code: str) -> str:
        async with self._lock:
            if self.state != "needs_2fa" or not self._pending_tfa:
                if self.state == "ready":
                    return "Already signed in to SmartRent."
                await self._login_with_password()
                if self.state != "needs_2fa":
                    return self.message or "Signed in."
            resp = await self.api.client._async_refresh_tokens_via_tfa(self._pending_tfa, code.strip())
            if resp.get("errors"):
                return "SmartRent didn't accept that code. Check it and try again, or wait for a new one."
            self._pending_tfa = None
            await self._accept_tokens(resp)
            return "Signed in to SmartRent." if self.state == "ready" else self.message

    async def _accept_tokens(self, resp: dict) -> None:
        if resp.get("errors"):
            self._set("error", "SmartRent rejected the login. Check SMARTRENT_EMAIL and SMARTRENT_PASSWORD.")
            return
        data = resp.get("data") or resp
        client = self.api.client
        client._token, client._refresh_token, client._token_exp_time = (
            data["access_token"], data["refresh_token"], data["expires"])
        await self._save_tokens()
        try:
            await self._load_devices()
        except Needs2FA:
            self._set("needs_2fa", "SmartRent wants a two-factor code.")
        except Exception as exc:
            self._set("error", f"Signed in, but couldn't load devices: {exc}")

    async def _load_devices(self) -> None:
        self.api._device_list = []               # the library appends; avoid duplicates on reconnect
        await self.api.async_fetch_devices()
        for d in self.api.get_device_list():
            d.start_updater()                    # live updates over SmartRent's websocket
            if self._on_change is not None:
                d.set_update_callback(self._changed(d))
        self._set("ready", f"{len(self.api.get_device_list())} device(s) connected.")

    def _changed(self, device):
        async def changed() -> None:
            try:
                await self._on_change(device)
            except Exception as exc:              # never let a report break SmartRent's updates
                log.warning("couldn't report %s's change: %s", getattr(device, "_name", "a device"), exc)
        return changed

    # ---- tokens ------------------------------------------------------------------------

    def _wrap_refresh(self) -> None:
        """Save tokens whenever the library refreshes them on its own."""
        client = self.api.client
        original = client._async_refresh_token

        async def refresh_and_save():
            await original()
            await self._save_tokens()

        client._async_refresh_token = refresh_and_save

    async def _save_tokens(self) -> None:
        c = self.api.client
        if c._refresh_token:
            await self.redis.set(TOKENS_KEY, json.dumps(
                {"access": c._token, "refresh": c._refresh_token, "expires": c._token_exp_time}))

    async def _restore_tokens(self) -> bool:
        raw = await self.redis.get(TOKENS_KEY)
        if not raw:
            return False
        t = json.loads(raw)
        c = self.api.client
        c._token, c._refresh_token, c._token_exp_time = t["access"], t["refresh"], t["expires"]
        return True

    async def forget_session(self) -> None:
        await self.redis.delete(TOKENS_KEY)

    # ---- helpers -----------------------------------------------------------------------

    def _set(self, state: str, message: str) -> None:
        self.state, self.message = state, message
        (log.info if state == "ready" else log.warning)("SmartRent: %s — %s", state, message)

    def devices(self) -> list[Any]:
        return self.api.get_device_list() if self.api and self.state == "ready" else []

    async def raw_devices(self) -> list[dict]:
        """Everything SmartRent reports, including device types the library doesn't model."""
        try:
            return await self.api.client.async_get_devices_data()
        except Exception:
            return []
