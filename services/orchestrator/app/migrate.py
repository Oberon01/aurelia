"""Apply pending SQL migrations in filename order, each in its own transaction.

Zero-downtime rule for every migration: EXPAND first (add tables/columns, nullable
or with defaults), deploy code that uses them, CONTRACT later (drop old columns)
in a separate migration. Old and new code must both work against the schema
while the deploy script runs them side by side.
"""
import asyncio
import logging
from pathlib import Path

import asyncpg

from .settings import settings

log = logging.getLogger("aurelia.migrate")
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
LOCK_ID = 7_202_609  # advisory lock so two migrators never run at once


async def run() -> None:
    conn = await asyncpg.connect(settings.database_url)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", LOCK_ID)
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in applied:
                continue
            log.info("applying %s", path.name)
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
        log.info("migrations up to date")
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_ID)
        await conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
