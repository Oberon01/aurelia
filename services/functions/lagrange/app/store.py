"""Connections in Postgres (tables made by the orchestrator's migration 0023_lagrange.sql), and their secrets
through the vault. Nothing here is kept only in memory: two copies of Lagrange run side by side during a
deploy, and either may answer."""
import json
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from .vault import Vault

COLS = ("id, name, label, kind, config, status, message, tools, pending, created_at, connected_at, updated_at, "
        "last_used_at, last_fired_at")
DRAFT_MINUTES = 30
SIGNIN_MINUTES = 10


async def _codecs(conn) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=5, init=_codecs)


def _row(r) -> dict[str, Any] | None:
    return dict(r) if r else None


class Store:
    def __init__(self, db: asyncpg.Pool, vault: Vault):
        self.db, self.vault = db, vault

    async def all(self, *, drafts: bool = False) -> list[dict]:
        rows = await self.db.fetch(f"SELECT {COLS} FROM connections {'' if drafts else 'WHERE status <> $1'} ORDER BY name",
                                   *([] if drafts else ["draft"]))
        return [dict(r) for r in rows]

    async def get(self, name: str) -> dict | None:
        return _row(await self.db.fetchrow(f"SELECT {COLS} FROM connections WHERE name=$1", name))

    async def draft(self, name: str, label: str, kind: str, config: dict) -> dict:
        """A new draft (replacing an older draft of the same name). A connection that exists stays as it is."""
        async with self.db.acquire() as c, c.transaction():
            old = await c.fetchrow("SELECT id, status FROM connections WHERE name=$1 FOR UPDATE", name)
            if old and old["status"] != "draft":
                raise ValueError(f"You already have a connection called {name}. Disconnect it first, or pick another name.")
            if old:                                 # checked again: the same row (and number), its old secrets gone
                await c.execute("DELETE FROM connection_secrets WHERE connection_id=$1", old["id"])
                await c.execute("DELETE FROM connection_signins WHERE connection_id=$1", old["id"])
                return dict(await c.fetchrow(f"UPDATE connections SET label=$2, kind=$3, config=$4, pending='[]', tools='[]', "
                                             f"message=NULL, updated_at=now() WHERE id=$1 RETURNING {COLS}",
                                             old["id"], label, kind, config))
            return dict(await c.fetchrow(f"INSERT INTO connections (name, label, kind, config) VALUES ($1,$2,$3,$4) "
                                         f"RETURNING {COLS}", name, label, kind, config))

    async def update(self, cid: int, **fields: Any) -> dict:
        sets = ", ".join(f"{k}=${i}" for i, k in enumerate(fields, 2))
        return dict(await self.db.fetchrow(f"UPDATE connections SET {sets}{', ' if sets else ''}updated_at=now() "
                                           f"WHERE id=$1 RETURNING {COLS}", cid, *fields.values()))

    async def set_status(self, cid: int, status: str, message: str | None, *, only_from: tuple[str, ...] | None = None) -> bool:
        """A status change that only lands from the statuses given (so a disconnect or a turn-off he made is
        never undone by a background check that started before it)."""
        q = "UPDATE connections SET status=$2, message=$3, updated_at=now() WHERE id=$1"
        args: list[Any] = [cid, status, message]
        if only_from:
            q += " AND status = ANY($4::text[])"
            args.append(list(only_from))
        return (await self.db.execute(q, *args)).endswith(" 1")

    async def delete(self, cid: int) -> None:
        await self.db.execute("DELETE FROM connections WHERE id=$1", cid)

    async def used(self, cid: int, *, fired: bool = False) -> None:
        await self.db.execute(f"UPDATE connections SET {'last_fired_at' if fired else 'last_used_at'}=now() WHERE id=$1", cid)

    # ---- secrets --------------------------------------------------------------------------------------------

    async def put_secret(self, cid: int, slot: str, value: str) -> None:
        nonce, box = self.vault.seal(cid, slot, value)
        await self.db.execute("INSERT INTO connection_secrets (connection_id, slot, nonce, box) VALUES ($1,$2,$3,$4) "
                              "ON CONFLICT (connection_id, slot) DO UPDATE SET nonce=$3, box=$4, updated_at=now()",
                              cid, slot, nonce, box)

    async def secret(self, cid: int, slot: str) -> str | None:
        r = await self.db.fetchrow("SELECT nonce, box FROM connection_secrets WHERE connection_id=$1 AND slot=$2", cid, slot)
        return self.vault.open(cid, slot, r["nonce"], r["box"]) if r else None

    async def secrets(self, cid: int) -> dict[str, str]:
        rows = await self.db.fetch("SELECT slot, nonce, box FROM connection_secrets WHERE connection_id=$1", cid)
        return {r["slot"]: self.vault.open(cid, r["slot"], r["nonce"], r["box"]) for r in rows}

    async def slots(self, cid: int) -> list[str]:
        return [r["slot"] for r in await self.db.fetch("SELECT slot FROM connection_secrets WHERE connection_id=$1 "
                                                       "ORDER BY slot", cid)]

    @asynccontextmanager
    async def locked(self, cid: int, wait: float = 30.0):
        """One holder at a time for this connection, across every copy of Lagrange (a Postgres advisory lock).
        Waiters don't hold a database connection while they wait, so the holder can always get one."""
        import asyncio
        import time
        key = 7_100_000_000 + int(cid)
        deadline = time.monotonic() + wait
        while True:
            c = await self.db.acquire()
            try:
                if await c.fetchval("SELECT pg_try_advisory_lock($1)", key):
                    break
            except BaseException:
                await self.db.release(c)
                raise
            await self.db.release(c)
            if time.monotonic() > deadline:
                raise TimeoutError("busy refreshing a sign-in")
            await asyncio.sleep(0.2)
        try:
            yield
        finally:
            try:
                await c.execute("SELECT pg_advisory_unlock($1)", key)
            finally:
                await self.db.release(c)

    async def oauth(self, cid: int) -> dict | None:
        raw = await self.secret(cid, "oauth")
        return json.loads(raw) if raw else None

    async def put_oauth(self, cid: int, saved: dict) -> None:
        await self.put_secret(cid, "oauth", json.dumps(saved))

    # ---- sign-ins ------------------------------------------------------------------------------------------

    async def signin_put(self, state: str, cid: int, verifier: str) -> None:
        nonce, box = self.vault.seal(cid, "signin:" + state, verifier)
        await self.db.execute("INSERT INTO connection_signins (state, connection_id, nonce, box) VALUES ($1,$2,$3,$4)",
                              state, cid, nonce, box)

    async def signin_take(self, state: str) -> tuple[int, str] | None:
        """The verifier for a state made in the last 10 minutes, used once."""
        r = await self.db.fetchrow("DELETE FROM connection_signins WHERE state=$1 AND created_at > now() - "
                                   "make_interval(mins => $2) RETURNING connection_id, nonce, box", state, SIGNIN_MINUTES)
        if not r:
            return None
        return r["connection_id"], self.vault.open(r["connection_id"], "signin:" + state, r["nonce"], r["box"])

    async def sweep(self) -> int:
        """Drafts he never connected (and their secrets) go after 30 minutes; old sign-in states after 10."""
        await self.db.execute("DELETE FROM connection_signins WHERE created_at < now() - make_interval(mins => $1)",
                              SIGNIN_MINUTES)
        r = await self.db.execute("DELETE FROM connections WHERE status='draft' AND updated_at < now() - "
                                  "make_interval(mins => $1)", DRAFT_MINUTES)
        return int(r.split()[-1])
