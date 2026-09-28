import asyncpg

_pool: asyncpg.Pool | None = None


async def connect(dsn: str) -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
    return _pool


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database not connected")
    return _pool


async def close() -> None:
    if _pool is not None:
        await _pool.close()
