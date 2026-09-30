"""The library in Postgres: topics and research. Tables are made by the orchestrator's
migrations (0010_kepler.sql); Kepler owns what's in them. It also reads (never
writes) the "learning" part of Kentrell's profile, to teach at his level."""
import json
from datetime import datetime, timezone
from typing import Any

from . import research as R

TOPIC_COLS = "id, name, summary, whats_new, disagreements, research_count, trilium_note_id, updated_at"


def vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def load(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


def topic_view(r) -> dict[str, Any]:
    return {"id": r["id"], "name": r["name"], "summary": r["summary"], "whats_new": r["whats_new"],
            "disagreements": load(r["disagreements"]) or [], "research_count": r["research_count"],
            "trilium_note_id": r["trilium_note_id"], "updated": r["updated_at"]}


def day(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%b %-d, %Y")


async def learning(pool, user: str, question: str, limit: int = 6) -> list[str]:
    """What he knows that bears on this (profile facts in 'learning'), most relevant first."""
    rows = await pool.fetch("SELECT fact FROM remembered_facts WHERE user_id=$1 AND category='learning' "
                            "ORDER BY updated_at DESC LIMIT 60", user)
    facts = [r["fact"] for r in rows]
    ranked = sorted(facts, key=lambda f: -R.word_match(f, question))
    return ranked[:limit]


async def find_topic(pool, user: str, text: str, embedding: list[float] | None, join_at: float,
                     words_at: float = 0.34) -> dict[str, Any] | None:
    """The topic this belongs to, if any: by name first (asking about "azure landing zones" means that topic),
    then by meaning, then by shared words."""
    rows = await pool.fetch(f"SELECT {TOPIC_COLS} FROM kepler_topics WHERE user_id=$1", user)
    named = max(rows, key=lambda r: R.word_match(text, r["name"]), default=None)
    if named is not None and R.word_match(text, named["name"]) >= 0.6:
        return topic_view(named)
    if embedding:
        r = await pool.fetchrow(
            f"SELECT {TOPIC_COLS}, 1 - (embedding <=> $2::vector) AS score FROM kepler_topics "
            "WHERE user_id=$1 AND embedding IS NOT NULL ORDER BY embedding <=> $2::vector LIMIT 1", user, vec(embedding))
        if r and r["score"] >= join_at:
            return topic_view(r)
    best, score = None, 0.0     # no embeddings, or not close enough in meaning: shared words
    for r in rows:
        s = max(R.word_match(text, r["name"]), R.word_match(text, r["summary"][:400]))
        if s > score:
            best, score = r, s
    return topic_view(best) if best is not None and score >= words_at else None


async def topic_names(pool, user: str, limit: int = 40) -> list[str]:
    """His topics' names, most recently grown first (so the writer can reuse one)."""
    return [r["name"] for r in await pool.fetch(
        "SELECT name FROM kepler_topics WHERE user_id=$1 ORDER BY updated_at DESC LIMIT $2", user, limit)]


async def topic_for_research(pool, user: str, name: str, doc_embedding: list[float] | None,
                             join_at: float) -> dict[str, Any] | None:
    """After the answer is written: the topic it names (same name, or nearly), else the topic whose
    summary is closest in meaning to the whole piece of research (document to document)."""
    rows = await pool.fetch(f"SELECT {TOPIC_COLS} FROM kepler_topics WHERE user_id=$1", user)
    key = " ".join(name.lower().split())
    for r in rows:
        if " ".join(r["name"].lower().split()) == key:
            return topic_view(r)
    named = max(rows, key=lambda r: R.word_match(name, r["name"]), default=None)
    if named is not None and name and R.word_match(name, named["name"]) >= 0.6:
        return topic_view(named)
    if doc_embedding:
        r = await pool.fetchrow(
            f"SELECT {TOPIC_COLS}, 1 - (embedding <=> $2::vector) AS score FROM kepler_topics "
            "WHERE user_id=$1 AND embedding IS NOT NULL ORDER BY embedding <=> $2::vector LIMIT 1", user, vec(doc_embedding))
        if r and r["score"] >= join_at:
            return topic_view(r)
    return None


async def topic_by_id(pool, user: str, topic_id: int) -> dict[str, Any] | None:
    r = await pool.fetchrow(f"SELECT {TOPIC_COLS} FROM kepler_topics WHERE id=$1 AND user_id=$2", topic_id, user)
    return topic_view(r) if r else None


async def research_in(pool, topic_id: int, limit: int = 20) -> list[dict[str, Any]]:
    rows = await pool.fetch("SELECT id, question, headline, key_points, sources, created_at FROM kepler_research "
                            "WHERE topic_id=$1 ORDER BY created_at DESC LIMIT $2", topic_id, limit)
    return [{"id": r["id"], "question": r["question"], "headline": R.plain(r["headline"]), "key_points": load(r["key_points"]) or [],
             "sources": load(r["sources"]) or [], "date": day(r["created_at"])} for r in rows]


async def save(pool, user: str, topic: dict[str, Any] | None, new_topic_name: str, merged: dict[str, Any],
               question: str, answer: dict[str, Any], sources: list[dict[str, Any]], depth: str, task_id: str | None,
               research_vec: list[float] | None, topic_vec: list[float] | None) -> tuple[int, int]:
    """(research id, topic id), in one transaction."""
    source_text = " ".join([question, answer["headline"], answer["answer"], *answer["key_points"]])[:6000]
    async with pool.acquire() as conn, conn.transaction():
        if topic:
            tid = topic["id"]
            await conn.execute(
                "UPDATE kepler_topics SET summary=$2, whats_new=$3, disagreements=$4::jsonb, research_count=research_count+1, "
                "embedding=COALESCE($5::vector, embedding), updated_at=now() WHERE id=$1",
                tid, merged["summary"], merged["whats_new"], json.dumps(merged["disagreements"]),
                vec(topic_vec) if topic_vec else None)
        else:
            tid = await conn.fetchval(
                "INSERT INTO kepler_topics (user_id, name, summary, disagreements, research_count, embedding) "
                "VALUES ($1, $2, $3, $4::jsonb, 1, $5::vector) RETURNING id", user, new_topic_name, merged["summary"],
                json.dumps(merged["disagreements"]), vec(topic_vec) if topic_vec else None)
        rid = await conn.fetchval(
            "INSERT INTO kepler_research (user_id, topic_id, question, headline, answer, key_points, disagreements, sources, "
            "depth, task_id, source_text, embedding) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8::jsonb, $9, $10, $11, "
            "$12::vector) RETURNING id", user, tid, question, answer["headline"], answer["answer"],
            json.dumps(answer["key_points"]), json.dumps(answer["disagreements"]), json.dumps(sources), depth,
            task_id, source_text, vec(research_vec) if research_vec else None)
    return rid, tid


async def set_note(pool, topic_id: int, note_id: str | None) -> None:
    await pool.execute("UPDATE kepler_topics SET trilium_note_id=$2 WHERE id=$1", topic_id, note_id)


async def search(pool, user: str, query: str, embedding: list[float] | None, limit: int = 6) -> dict[str, Any]:
    if embedding:
        research = await pool.fetch(
            "SELECT r.id, r.question, r.headline, r.created_at, t.name AS topic, t.id AS topic_id FROM kepler_research r "
            "LEFT JOIN kepler_topics t ON t.id=r.topic_id WHERE r.user_id=$1 AND r.embedding IS NOT NULL "
            "ORDER BY r.embedding <=> $2::vector LIMIT $3", user, vec(embedding), limit)
        topics = await pool.fetch(
            f"SELECT {TOPIC_COLS} FROM kepler_topics WHERE user_id=$1 AND embedding IS NOT NULL "
            "ORDER BY embedding <=> $2::vector LIMIT 3", user, vec(embedding))
        how = "meaning"
    else:
        ws = sorted(R.words(query))[:8] or [query.lower()]
        research = await pool.fetch(
            "SELECT r.id, r.question, r.headline, r.created_at, t.name AS topic, t.id AS topic_id, "
            "(SELECT count(*) FROM unnest($2::text[]) w WHERE lower(r.source_text) LIKE '%' || w || '%') AS score "
            "FROM kepler_research r LEFT JOIN kepler_topics t ON t.id=r.topic_id WHERE r.user_id=$1 "
            "AND lower(r.source_text) LIKE ANY (SELECT '%' || w || '%' FROM unnest($2::text[]) w) "
            "ORDER BY score DESC, r.created_at DESC LIMIT $3", user, ws, limit)
        topics = await pool.fetch(
            f"SELECT {TOPIC_COLS} FROM kepler_topics WHERE user_id=$1 AND (lower(name) LIKE ANY "
            "(SELECT '%' || w || '%' FROM unnest($2::text[]) w)) ORDER BY updated_at DESC LIMIT 3", user, ws)
        how = "words"
    return {"matched_by": how,
            "topics": [{"id": t["id"], "name": t["name"], "summary": t["summary"][:400], "research_count": t["research_count"],
                        "updated": day(t["updated_at"])} for t in topics],
            "research": [{"id": r["id"], "question": r["question"], "headline": R.plain(r["headline"]), "date": day(r["created_at"]),
                          "topic": r["topic"], "topic_id": r["topic_id"]} for r in research]}


async def counts(pool, user: str = "owner") -> dict[str, int]:
    r = await pool.fetchrow("SELECT (SELECT count(*) FROM kepler_topics WHERE user_id=$1) AS topics, "
                            "(SELECT count(*) FROM kepler_research WHERE user_id=$1) AS research", user)
    return {"topics": r["topics"], "research": r["research"]}


async def delete_research(pool, user: str, research_id: int) -> tuple[str, int | None, dict[str, Any] | None]:
    """Deletes one piece of research. Returns (its question, its topic id, the topic if it's now empty and removed)."""
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("DELETE FROM kepler_research WHERE id=$1 AND user_id=$2 RETURNING question, topic_id",
                                  research_id, user)
        if row is None:
            raise R.KeplerError("I don't have that research.")
        gone = None
        if row["topic_id"]:
            left = await conn.fetchval("UPDATE kepler_topics SET research_count=GREATEST(research_count-1, 0), updated_at=now() "
                                       "WHERE id=$1 RETURNING research_count", row["topic_id"])
            if left == 0:
                t = await conn.fetchrow(f"DELETE FROM kepler_topics WHERE id=$1 RETURNING {TOPIC_COLS}", row["topic_id"])
                gone = topic_view(t) if t else None
    return row["question"], row["topic_id"], gone


async def delete_topic(pool, user: str, topic_id: int) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        t = await conn.fetchrow(f"SELECT {TOPIC_COLS} FROM kepler_topics WHERE id=$1 AND user_id=$2", topic_id, user)
        if t is None:
            raise R.KeplerError("I don't have that topic.")
        await conn.execute("DELETE FROM kepler_research WHERE topic_id=$1", topic_id)
        await conn.execute("DELETE FROM kepler_topics WHERE id=$1", topic_id)
    return topic_view(t)
