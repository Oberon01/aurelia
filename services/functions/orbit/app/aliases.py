"""Your names for devices, saved in Redis and managed through Orbit's tools.

"remember that the living room light is Switch 2" → orbit.alias. Takes effect
immediately; no config change, commit or deploy. Aliases in Orbit's policy
file (if any) still work too; saved ones take precedence.
"""
import json
import re

ALIASES_KEY = "aurelia:orbit:aliases"


def alias_key(text: str) -> str:
    return " ".join(re.sub(r"[^\w/ ]+", " ", text.lower()).split()).removeprefix("the ").removeprefix("my ")


class AliasStore:
    def __init__(self, redis):
        self.redis = redis

    async def all(self) -> dict[str, str]:
        """{what you say: device name in SmartRent}"""
        raw = await self.redis.hgetall(ALIASES_KEY)
        out = {}
        for v in raw.values():
            entry = json.loads(v)
            out[entry["said"]] = entry["device"]
        return out

    async def set(self, said: str, device: str) -> None:
        await self.redis.hset(ALIASES_KEY, alias_key(said), json.dumps({"said": said, "device": device}))

    async def forget(self, said: str) -> bool:
        return bool(await self.redis.hdel(ALIASES_KEY, alias_key(said)))
