"""Beacon's tables (migration 0024): sealed secrets, plain settings, the week's email headers, people you've
written to, and the next two weeks of your calendars."""
import json
from datetime import datetime
from typing import Any

import asyncpg

from .vault import Vault


async def pool(url: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(url, min_size=1, max_size=5)


class Store:
    def __init__(self, db: asyncpg.Pool, vault: Vault):
        self.db, self.vault = db, vault

    # ---- secrets and settings ----------------------------------------------------------------------------

    async def put_secret(self, slot: str, value: str) -> None:
        nonce, box = self.vault.seal(slot, value)
        await self.db.execute("INSERT INTO beacon_secrets (slot, nonce, box) VALUES ($1, $2, $3) ON CONFLICT (slot) "
                              "DO UPDATE SET nonce=$2, box=$3, updated_at=now()", slot, nonce, box)

    async def secret(self, slot: str) -> str | None:
        row = await self.db.fetchrow("SELECT nonce, box FROM beacon_secrets WHERE slot=$1", slot)
        return self.vault.open(slot, row["nonce"], row["box"]) if row else None

    async def forget(self, slot: str) -> None:
        await self.db.execute("DELETE FROM beacon_secrets WHERE slot=$1", slot)

    async def slots(self) -> set[str]:
        return {r["slot"] for r in await self.db.fetch("SELECT slot FROM beacon_secrets")}

    async def setting(self, key: str, default: Any = None) -> Any:
        v = await self.db.fetchval("SELECT value FROM beacon_settings WHERE key=$1", key)
        return default if v is None else (json.loads(v) if isinstance(v, str) else v)

    async def set_setting(self, key: str, value: Any) -> None:
        await self.db.execute("INSERT INTO beacon_settings (key, value) VALUES ($1, $2::jsonb) ON CONFLICT (key) "
                              "DO UPDATE SET value=$2::jsonb, updated_at=now()", key, json.dumps(value))

    async def drop_setting(self, key: str) -> None:
        await self.db.execute("DELETE FROM beacon_settings WHERE key=$1", key)

    # ---- email --------------------------------------------------------------------------------------------

    async def known_ids(self, ids: list[str]) -> set[str]:
        rows = await self.db.fetch("SELECT msg_id FROM mail_items WHERE msg_id = ANY($1::text[])", ids)
        return {r["msg_id"] for r in rows}

    async def add_mail(self, m: dict) -> None:
        await self.db.execute(
            "INSERT INTO mail_items (msg_id, uid, uidvalidity, from_addr, from_name, subject, received_at, snippet, "
            "important, why) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (msg_id) DO UPDATE SET uid=$2, "
            "uidvalidity=$3", m["msg_id"], m["uid"], m["uidvalidity"], m["from_addr"][:300], m["from_name"][:200],
            m["subject"][:500], m["received_at"], m["snippet"][:600], bool(m["important"]), (m.get("why") or "")[:200])

    async def mail(self, since: datetime, important_only: bool = False, limit: int = 50) -> list[dict]:
        rows = await self.db.fetch(
            "SELECT * FROM mail_items WHERE received_at >= $1" + (" AND important" if important_only else "") +
            " ORDER BY received_at DESC LIMIT $2", since, limit)
        return [dict(r) for r in rows]

    async def mail_item(self, msg_id: str) -> dict | None:
        row = await self.db.fetchrow("SELECT * FROM mail_items WHERE msg_id=$1", msg_id)
        return dict(row) if row else None

    async def prune_mail(self, before: datetime) -> None:
        await self.db.execute("DELETE FROM mail_items WHERE received_at < $1", before)

    async def add_contacts(self, addrs: dict[str, datetime]) -> None:
        if addrs:
            await self.db.executemany(
                "INSERT INTO mail_contacts (addr, last_at) VALUES ($1, $2) ON CONFLICT (addr) DO UPDATE "
                "SET last_at=GREATEST(mail_contacts.last_at, $2)", [(a.lower(), t) for a, t in addrs.items()])

    async def is_contact(self, addr: str) -> bool:
        return bool(await self.db.fetchval("SELECT 1 FROM mail_contacts WHERE addr=$1", (addr or "").lower()))

    # ---- calendars ----------------------------------------------------------------------------------------

    async def replace_events(self, source: str, start: datetime, end: datetime, events: list[dict]) -> None:
        """This source's events in [start, end), as they are now (one transaction: readers never see it empty)."""
        async with self.db.acquire() as c, c.transaction():
            await c.execute("DELETE FROM calendar_events WHERE source=$1 AND starts_at < $3 AND "
                            "COALESCE(ends_at, starts_at) >= $2", source, start, end)
            if events:
                await c.executemany(
                    "INSERT INTO calendar_events (source, uid, starts_at, ends_at, all_day, title, location) VALUES "
                    "($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (source, uid, starts_at) DO UPDATE SET ends_at=$4, "
                    "all_day=$5, title=$6, location=$7",
                    [(source, e["uid"][:300], e["start"], e.get("end"), bool(e.get("all_day")),
                      (e.get("title") or "")[:300], (e.get("location") or "")[:300]) for e in events])
