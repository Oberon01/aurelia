"""Release records in Postgres (table `releases`, migration 0004)."""
import json
from typing import Any

import asyncpg

SKIP_STATUSES = ("failed", "rolled_back", "held", "dry_run")


class Store:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create(self, sha: str, prev_sha: str | None, author: str, message: str,
                     paths: list[str], plan: dict[str, Any], status: str) -> int:
        return await self.pool.fetchval(
            "INSERT INTO releases (sha, prev_sha, author, message, changed_paths, services, plan, status) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8) RETURNING id",
            sha, prev_sha, author, message, paths, plan.get("services", []), json.dumps(plan), status)

    async def update(self, release_id: int, status: str, detail: str | None = None, finished: bool = False) -> None:
        await self.pool.execute(
            "UPDATE releases SET status=$2, detail=COALESCE($3, detail), "
            "finished_at=CASE WHEN $4 THEN now() ELSE finished_at END WHERE id=$1",
            release_id, status, detail, finished)

    async def decided(self, sha: str, dry_run: bool) -> str | None:
        """Status of an earlier decision about this exact commit, if it should not be retried.
        A dry-run record only blocks repeats while still in dry-run mode."""
        skip = list(SKIP_STATUSES) if dry_run else [s for s in SKIP_STATUSES if s != "dry_run"]
        return await self.pool.fetchval(
            "SELECT status FROM releases WHERE sha=$1 AND status = ANY($2::text[]) ORDER BY id DESC LIMIT 1",
            sha, skip)

    async def approved(self, sha: str) -> bool:
        return bool(await self.pool.fetchval(
            "SELECT 1 FROM releases WHERE sha=$1 AND status='approved' LIMIT 1", sha))

    async def live(self, limit: int) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT * FROM releases WHERE status='live' ORDER BY id DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def recent(self, limit: int = 10) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT id, sha, author, message, services, status, detail, started_at, finished_at "
            "FROM releases ORDER BY id DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def get(self, release_id: int) -> dict | None:
        row = await self.pool.fetchrow("SELECT * FROM releases WHERE id=$1", release_id)
        return dict(row) if row else None
