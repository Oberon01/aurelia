"""Login, two-factor codes, and saved sessions — without contacting SmartRent."""
import asyncio

import fakeredis.aioredis

from app.home import TOKENS_KEY, Home

from .fakes import my_home

TOKENS = {"access_token": "a1", "refresh_token": "r1", "expires": 9999999999}


class FakeClient:
    def __init__(self, needs_tfa, valid_codes=("123456",), accept_saved=True):
        self._token = self._refresh_token = self._token_exp_time = None
        self.needs_tfa, self.valid_codes, self.accept_saved = needs_tfa, valid_codes, accept_saved
        self.email_logins = 0

    async def _async_refresh_token(self):
        self._refresh_token = "r2"

    async def _async_refresh_tokens_via_email(self):
        self.email_logins += 1
        return {"tfa_api_token": "tfa-xyz"} if self.needs_tfa else dict(TOKENS)

    async def _async_refresh_tokens_via_tfa(self, tfa_api_token, code):
        assert tfa_api_token == "tfa-xyz"
        return {"data": dict(TOKENS)} if code in self.valid_codes else {"errors": [{"code": "invalid"}]}

    async def async_get_devices_data(self):
        return []


class FakeAPI:
    def __init__(self, client):
        self.client, self._device_list = client, []

    async def async_fetch_devices(self):
        if self.client._token == "stale":
            raise RuntimeError("401")
        self._device_list.extend(my_home())

    def get_device_list(self): return self._device_list


def make(redis, client):
    return Home("me@example.com", "pw", redis, api_factory=lambda e, p: FakeAPI(client))


def test_two_factor_flow_and_session_saved():
    async def go():
        redis = fakeredis.aioredis.FakeRedis()
        client = FakeClient(needs_tfa=True)
        h = make(redis, client)
        await h.start()
        assert h.state == "needs_2fa"
        assert "didn't accept" in await h.complete_2fa("000000")
        assert h.state == "needs_2fa"
        assert await h.complete_2fa("123456") == "Signed in to SmartRent."
        assert h.state == "ready" and len(h.devices()) == 5
        assert all(d.updater_started for d in h.devices())
        assert await redis.get(TOKENS_KEY)

        # A restart (every deploy) reuses the saved session: no login, no 2FA.
        client2 = FakeClient(needs_tfa=True)
        h2 = make(redis, client2)
        await h2.start()
        assert h2.state == "ready" and client2.email_logins == 0
    asyncio.run(go())


def test_expired_saved_session_falls_back_to_login():
    async def go():
        redis = fakeredis.aioredis.FakeRedis()
        await redis.set(TOKENS_KEY, '{"access": "stale", "refresh": "old", "expires": 1}')
        client = FakeClient(needs_tfa=False)
        h = make(redis, client)
        await h.start()
        assert h.state == "ready" and client.email_logins == 1
    asyncio.run(go())


def test_library_refreshes_are_saved():
    async def go():
        redis = fakeredis.aioredis.FakeRedis()
        client = FakeClient(needs_tfa=False)
        h = make(redis, client)
        await h.start()
        await client._async_refresh_token()          # what the library calls on its own
        assert b'"r2"' in await redis.get(TOKENS_KEY)
    asyncio.run(go())


def test_missing_credentials():
    async def go():
        h = Home(None, None, fakeredis.aioredis.FakeRedis())
        await h.start()
        assert h.state == "no_credentials" and "SMARTRENT_EMAIL" in h.message
    asyncio.run(go())


def test_library_prompt_is_replaced():
    """smartrent-py would call input() for a 2FA code; that must never block."""
    import smartrent.utils
    from app.home import Needs2FA

    async def go():
        h = Home("me@example.com", "pw", fakeredis.aioredis.FakeRedis())
        api = h._make_api()                      # the real library client
        try:
            smartrent.utils.input("code?")
            raise AssertionError("prompt was not replaced")
        except Needs2FA:
            pass
        await api.client._aiohttp_session.close()
    asyncio.run(go())


def test_a_device_changing_is_reported_to_pulsar():
    """SmartRent's live update for a device reaches whoever listens (Orbit tells Pulsar); a failing report
    never breaks SmartRent's updates."""
    redis = fakeredis.aioredis.FakeRedis()
    heard = []

    async def on_change(device):
        heard.append(device.get_name())
        if len(heard) == 2:
            raise RuntimeError("orchestrator down")

    home = Home("me@example.com", "pw", redis, api_factory=lambda e, p: FakeAPI(FakeClient(needs_tfa=False)),
                on_change=on_change)
    asyncio.run(home.start())
    assert home.state == "ready"
    lock = next(d for d in home.devices() if d.get_name() == "Front Door")
    assert len(lock.callbacks) == 1
    asyncio.run(lock.callbacks[0]())
    asyncio.run(lock.callbacks[0]())                         # the report fails: logged, nothing raised
    assert heard == ["Front Door", "Front Door"]
