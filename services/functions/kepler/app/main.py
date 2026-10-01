"""Kepler — Aurelia's research and learning function (Phase 6).

Tools:
- research  a question answered from 3 (quick) or 8 (thorough) web sources, cited
- recall    what he's learned before, by meaning ("what did I learn about X")
- topic     one topic: the combined understanding across its research
- explain   an explanation at his level (from his profile's "learning" facts)
- quiz      multiple-choice questions to check understanding; the display reports
            the score to the orchestrator, which may propose a profile fact
- read_pdf  a document as text: a PDF link, or a PDF, Word or text file from his downloads or file server
- forget    delete a piece of research or a whole topic (confirmed)

How it reaches the world, on purpose:
- the web only through Nebula, as hidden tasks via the orchestrator (the same
  governance and network guard as everything else);
- models and embeddings only through the orchestrator (no API key here; every
  call counts toward the monthly cap and is logged);
- his Trilium directly, one-way: topics are written as notes he can read.
"""
import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pydantic_settings import BaseSettings

from . import documents
from . import research as R
from . import store
from .research import KeplerError
from .trilium import Trilium

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("function.kepler")


class Settings(BaseSettings):
    orchestrator_url: str = "http://orchestrator:8000"
    function_url: str = "http://kepler:9000"
    database_url: str = ""
    heartbeat_seconds: int = 10
    trilium_url: str | None = None
    trilium_token: str | None = None
    downloads_dir: str = "/downloads"            # his downloads, mounted read-only
    files_dir: str = "/files"                    # his file server, mounted read-only (optional)


settings = Settings()

TOOLS_SPEC = [
    {"name": "research", "description": "Research a question on the web and answer it with numbered citations, "
                                        "from 3 sources (quick, the default) or 8 (thorough: when he says research it "
                                        "properly, in depth, thoroughly). Saves it to his library under a topic.",
     "input_schema": {"type": "object", "properties": {
         "question": {"type": "string"}, "depth": {"type": "string", "enum": ["quick", "thorough"]}},
         "required": ["question"]}, "progress_unit": "steps", "freshness_seconds": 86400},
    {"name": "recall", "description": "His research library. With a query, search it by meaning: 'what did I learn "
                                      "about X', 'have I looked into Y'. Without a query, the whole library: 'what topics "
                                      "have I learned', 'show my library'. (Conversations are Mnemosyne's recall.)",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string", "description": "leave out for the whole library"}}}},
    {"name": "topic", "description": "Show one topic from his library: the combined understanding, what's new, open "
                                     "questions, and the research in it. By name or id.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "id": {"type": "integer"}}}},
    {"name": "explain", "description": "Explain a subject at his level, building on what he already knows (and on his "
                                       "library, if he's researched it). Use for 'explain X', 'help me understand X'.",
     "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}}, "required": ["subject"]}},
    {"name": "quiz", "description": "Quiz him with multiple-choice questions to check understanding of a subject or "
                                    "library topic ('quiz me on X'). The panel is interactive; don't reveal answers.",
     "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "topic_id": {"type": "integer"},
                                                       "count": {"type": "integer", "minimum": 3, "maximum": 10}},
                      "required": ["subject"]}},
    {"name": "read_pdf", "description": "Read a document as text: a PDF link (url), or a PDF, Word (.docx) or text "
                                        "file from his downloads or his file server (file: its name, a path inside "
                                        "the folder, or words from its name, e.g. 'azure notes'). If more than one "
                                        "matches, ask him which.",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}, "file": {"type": "string"}}},
     "progress_unit": "steps"},
    {"name": "forget", "description": "Delete a whole topic from his library by its name (topic), or by id (topic_id), "
                                      "or one piece of research (research_id), including the Trilium note. Prefer the "
                                      "topic's name. Needs his confirmation.",
     "input_schema": {"type": "object", "properties": {"topic": {"type": "string", "description": "the topic's name"},
                                                       "research_id": {"type": "integer"}, "topic_id": {"type": "integer"}}}},
]
REFRESHES = {"forget": ["kepler.recall", "kepler.topic"]}
PANEL_TOOLS = {"research", "recall", "topic", "explain", "quiz"}      # always shown as a panel, never "brief"
REGISTRATION = {"name": "kepler", "description": "Research and learning: cited answers, a library of what he's learned, "
                                                 "explanations at his level, and quizzes.",
                "url": settings.function_url,
                "tools": [{**t, "cancellable": t["name"] in ("research", "read_pdf"), "refreshes": REFRESHES.get(t["name"], []),
                           "display": "panel" if t["name"] in PANEL_TOOLS else "choice"}
                          for t in TOOLS_SPEC]}

POLICY: dict = {}
DEFAULT_RULES = {"quick_sources": 3, "thorough_sources": 8, "topic_join": 0.78, "max_per_site": 2,
                 "read_timeout_seconds": 45, "quiz_questions": 5, "trilium": {"enabled": True, "parent_note": "root", "wake_seconds": 60, "retry_minutes": 5}}
_cancelled: set[str] = set()


def rules() -> dict[str, Any]:
    r = (POLICY.get("policy") or {}).get("rules") or {}
    return {**DEFAULT_RULES, **r, "trilium": {**DEFAULT_RULES["trilium"], **(r.get("trilium") or {})}}


# ---- the orchestrator: models, embeddings, and Nebula ------------------------------------

class Orchestrator:
    def __init__(self, http: httpx.AsyncClient, user: str):
        self.http, self.user = http, user

    async def llm(self, system: str, prompt: str, purpose: str = "research") -> str:
        r = await self.http.post(f"{settings.orchestrator_url}/llm/complete", timeout=180, json={
            "purpose": purpose, "system": system, "messages": [{"role": "user", "text": prompt}], "function": "kepler"})
        if r.status_code in (429, 503):
            raise KeplerError(r.json().get("detail", "No model is available right now."))
        r.raise_for_status()
        return r.json()["text"]

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        try:
            r = await self.http.post(f"{settings.orchestrator_url}/llm/embed", json={"texts": texts}, timeout=60)
            return r.json().get("embeddings") if r.is_success else None
        except httpx.HTTPError:
            return None

    async def run(self, tool: str, args: dict, stop: Callable[[], bool], timeout: float = 45) -> dict:
        """A Nebula tool as a hidden task (no panel of its own), through the same checks as any task."""
        r = await self.http.post(f"{settings.orchestrator_url}/tasks", json={
            "tool": tool, "args": args, "user": self.user, "trigger": "kepler", "display": "brief"})
        if r.status_code != 202:
            detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else r.text
            raise KeplerError(f"{tool} refused: {detail.get('reason', detail) if isinstance(detail, dict) else detail}")
        task_id = r.json()["task_id"]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if stop():
                try:
                    await self.http.post(f"{settings.orchestrator_url}/tasks/{task_id}/cancel")
                except httpx.HTTPError:
                    pass
                raise asyncio.CancelledError
            row = (await self.http.get(f"{settings.orchestrator_url}/tasks/{task_id}")).json()
            if row["status"] != "running":
                if row["status"] == "failed":
                    raise KeplerError(row.get("error") or f"{tool} failed")
                res = row.get("result")
                return (json.loads(res) if isinstance(res, str) else res) or {}
            await asyncio.sleep(0.3)
        raise KeplerError(f"{tool} took too long")


# ---- tools ----------------------------------------------------------------------------------

class TaskContext:
    def __init__(self, task_id: str, args: dict, http: httpx.AsyncClient, user: str):
        self.task_id, self.args, self._http = task_id, args, http
        self.orch = Orchestrator(http, user)
        self.user = user
        self._base = f"{settings.orchestrator_url}/tasks/{task_id}"

    @property
    def cancelled(self) -> bool:
        return self.task_id in _cancelled

    async def progress(self, current: float, total: float | None, label: str | None = None) -> None:
        try:
            r = await self._http.post(f"{self._base}/progress", json={"current": current, "total": total,
                                                                       "unit": "steps", "label": label})
            if r.is_success and r.json().get("cancel"):
                _cancelled.add(self.task_id)
        except httpx.HTTPError:
            pass


def trilium() -> Trilium:
    return Trilium(settings.trilium_url, settings.trilium_token, wake_seconds=float(rules()["trilium"]["wake_seconds"]))


async def research(ctx: TaskContext) -> dict:
    question = " ".join(str(ctx.args.get("question") or "").split())[:500]
    if not question:
        raise KeplerError("What should I research?")
    depth = R.THOROUGH if ctx.args.get("depth") == R.THOROUGH else R.QUICK
    ru, pool, o = rules(), app.state.db, ctx.orch
    want = int(ru["thorough_sources" if depth == R.THOROUGH else "quick_sources"])
    total = want + 4                                          # plan, search, read ×n, write, file

    # 1. What to search for: the question itself for a quick look; a few angles for a thorough one.
    await ctx.progress(0, total, "Planning")
    queries = R.queries_from(await o.llm(R.PLAN, question), question) if depth == R.THOROUGH else [question]

    # 2. Search, then pick sources across the angles.
    await ctx.progress(1, total, "Searching the web")
    lists = []
    for q in queries:
        try:
            lists.append((await o.run("nebula.search", {"query": q, "count": 8}, lambda: ctx.cancelled)).get("results") or [])
        except KeplerError as exc:
            log.info("search %r failed: %s", q, exc)
    candidates = R.pick_sources(lists, int(ru["max_per_site"]))
    if not candidates:
        raise KeplerError("The web search didn't turn up anything I could read.")

    # 3. Read them, three at a time; a page that fails is replaced by the next candidate.
    sources: list[dict[str, Any]] = []
    queue = list(candidates)
    while queue and len(sources) < want:
        batch, queue = queue[: min(3, want - len(sources))], queue[min(3, want - len(sources)):]
        pages = await asyncio.gather(*[o.run("nebula.read", {"url": c["url"]}, lambda: ctx.cancelled,
                                              float(ru["read_timeout_seconds"])) for c in batch], return_exceptions=True)
        for c, page in zip(batch, pages):
            if isinstance(page, asyncio.CancelledError):
                raise page
            if isinstance(page, Exception) or not (page or {}).get("text"):
                continue
            sources.append({**c, "n": len(sources) + 1, "title": page.get("title") or c["title"], "text": page["text"]})
            await ctx.progress(1 + len(sources), total, f"Reading {len(sources)} of {want}")
    if not sources:
        raise KeplerError("I couldn't read any of the pages I found.")

    # 4. Write the answer, at his level, knowing what he learned on this before.
    await ctx.progress(want + 2, total, "Writing")
    learning = await store.learning(pool, ctx.user, question)
    q_vec = (await o.embed([question]) or [None])[0]
    topic = await store.find_topic(pool, ctx.user, question, q_vec, float(ru["topic_join"]))
    topics = [] if topic else await store.topic_names(pool, ctx.user)
    answer = R.read_answer(await o.llm(R.WRITE, R.write_prompt(question, sources, depth, learning,
                                                               topic["summary"] if topic else None, topics)),
                           {s["n"] for s in sources})
    srcs = R.cited(answer, sources)
    doc_vec = (await o.embed([" ".join([question, answer["headline"], answer["answer"][:1500]])]) or [None])[0]
    if not topic and topics:
        # The question alone didn't find it: try the topic the answer names, then the whole research's meaning.
        topic = await store.topic_for_research(pool, ctx.user, answer["topic"], doc_vec, float(ru["topic_join"]))

    # 5. File it: into its topic (merging what's known), then the one-way Trilium note.
    await ctx.progress(want + 3, total, "Adding to your library")
    if topic:
        merged = R.read_merge(await o.llm(R.MERGE, R.merge_prompt(topic["name"], topic["summary"], topic["disagreements"],
                                                                  question, answer)), topic["summary"])
        earlier = await store.research_in(pool, topic["id"], 3)
    else:
        merged = {"summary": R.first_summary(answer), "whats_new": "", "disagreements": answer["disagreements"]}
        earlier = []
    name = topic["name"] if topic else (answer["topic"] or question[:60])
    topic_vec = (await o.embed([f"{name}. {merged['summary']}"]) or [None])[0]
    vecs = [doc_vec, topic_vec] if doc_vec or topic_vec else []
    rid, tid = await store.save(pool, ctx.user, topic, name, merged, question, answer, srcs, depth, ctx.task_id,
                                vecs[0] if vecs else None, vecs[1] if vecs else None)
    note = await sync_note(pool, ctx.user, tid)
    return {"headline": R.plain(answer["headline"]), "research": {
        "id": rid, "question": question, "depth": depth, "answer": answer["answer"], "key_points": answer["key_points"],
        "disagreements": answer["disagreements"], "sources": srcs,
        "topic": {"id": tid, "name": name, "joined": bool(topic), "research_count": (topic["research_count"] + 1) if topic else 1,
                  "whats_new": merged["whats_new"] if topic else "", "earlier": [{"date": e["date"], "question": e["question"]}
                                                                                   for e in earlier]},
        "trilium": note}}


_sync_lock = asyncio.Lock()      # one writer to Trilium at a time (never two new notes for one topic)


async def sync_note(pool, user: str, topic_id: int) -> str:
    """Writes the topic to Trilium: 'saved', 'off', 'not set up', or 'failed' (never fails the research).
    The library is the record: if Trilium can't be reached, the topic is marked and the catch-up round
    writes it once Trilium answers again."""
    t = trilium()
    if not rules()["trilium"].get("enabled", True):
        return "off"
    if not t.ready:
        return "not set up"
    async with _sync_lock:
        topic = await store.topic_by_id(pool, user, topic_id)
        if topic is None:
            return "off"                                      # forgotten meanwhile
        try:
            note = await t.upsert(topic["trilium_note_id"], topic["name"],
                                  R.trilium_html(topic, await store.research_in(pool, topic_id, 30)),
                                  rules()["trilium"].get("parent_note") or "root")
            await store.set_note(pool, topic_id, note)        # clears pending
            return "saved"
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("couldn't write topic %s to Trilium (will retry): %s", topic_id, exc)
            await store.set_pending(pool, topic_id, True)
            return "failed"


async def catch_up(pool) -> dict[str, int]:
    """Writes every topic Trilium is behind on, and removes notes of forgotten topics. Only talks to
    Trilium when something is waiting (so it never keeps a sleeping Trilium awake), and stops at the
    first failure: Trilium is still away, the next round tries again."""
    done = {"written": 0, "removed": 0}
    t = trilium()
    if not rules()["trilium"].get("enabled", True) or not t.ready:
        return done
    for note_id in await store.pending_deletes(pool):
        try:
            async with _sync_lock:
                await t.delete(note_id)
        except httpx.HTTPError as exc:
            log.info("Trilium still unreachable (%s); will retry", exc.__class__.__name__)
            return done
        await store.delete_done(pool, note_id)
        done["removed"] += 1
    for user, topic_id in await store.pending_topics(pool):
        if await sync_note(pool, user, topic_id) == "failed":
            return done
        done["written"] += 1
    if done["written"] or done["removed"]:
        log.info("Trilium caught up: %s topic(s) written, %s note(s) removed", done["written"], done["removed"])
    return done


async def catch_up_loop() -> None:
    await asyncio.sleep(30)                                   # soon after start: picks up anything left waiting
    while True:
        try:
            await catch_up(app.state.db)
        except Exception:                                     # never let the loop die
            log.exception("Trilium catch-up failed")
        await asyncio.sleep(max(1.0, float(rules()["trilium"].get("retry_minutes", 5))) * 60)


async def recall(ctx: TaskContext) -> dict:
    q = " ".join(str(ctx.args.get("query") or "").split())[:300]
    if q.lower() in ("all", "everything", "topics", "my topics", "library", "my library", "*"):
        q = ""
    if q:
        vec = (await ctx.orch.embed([q]) or [None])[0]
        found = await store.search(app.state.db, ctx.user, q, vec)
    else:
        found = await store.everything(app.state.db, ctx.user)
    out = {"library": {"query": q, **found}}
    if not found["topics"] and not found["research"]:
        out["library"]["message"] = out["message"] = ("Nothing in your library on that yet. Ask me to research it." if q else
                                                      "Your library is empty so far. Ask me to research something.")
    return out


async def show_topic(ctx: TaskContext) -> dict:
    pool = app.state.db
    if ctx.args.get("id") is not None:
        t = await store.topic_by_id(pool, ctx.user, int(ctx.args["id"]))
    else:
        name = " ".join(str(ctx.args.get("name") or "").split())
        if not name:
            raise KeplerError("Which topic?")
        vec = (await ctx.orch.embed([name]) or [None])[0]
        t = await store.find_topic(pool, ctx.user, name, vec, 0.6, 0.2)
    if not t:
        raise KeplerError("I don't have a topic like that yet.")
    items = await store.research_in(pool, t["id"], 30)
    seen, sources = set(), []
    for r in items:
        for s in r["sources"]:
            if s.get("cited", True) and s["url"] not in seen:
                seen.add(s["url"]); sources.append({"title": s["title"], "url": s["url"], "site": s["site"]})
    return {"headline": f"{len(items)} piece{'s' if len(items) != 1 else ''} of research · updated {store.day(t['updated'])}",
            "topic": {"id": t["id"], "name": t["name"], "summary": t["summary"], "whats_new": t["whats_new"],
                      "disagreements": t["disagreements"], "research": [{k: r[k] for k in ("id", "question", "headline", "date")}
                                                                        for r in items],
                      "sources": sources[:15], "in_trilium": bool(t["trilium_note_id"])}}


async def _grounding(ctx: TaskContext, subject: str) -> tuple[dict | None, list[str]]:
    vec = (await ctx.orch.embed([subject]) or [None])[0]
    t = await store.find_topic(app.state.db, ctx.user, subject, vec, 0.62, 0.3)
    points = [p for r in (await store.research_in(app.state.db, t["id"], 6) if t else []) for p in r["key_points"]]
    return t, points


async def explain(ctx: TaskContext) -> dict:
    subject = " ".join(str(ctx.args.get("subject") or "").split())[:300]
    if not subject:
        raise KeplerError("What should I explain?")
    t, points = await _grounding(ctx, subject)
    learning = await store.learning(app.state.db, ctx.user, subject)
    e = R.read_explain(await ctx.orch.llm(R.EXPLAIN, f"Subject: {subject}\n\n{R.level_note(learning)}\n\n{R.grounding(t, points)}"))
    return {"headline": e["headline"] or subject, "explain": {"subject": subject, **e,
                                                               "from_library": t["name"] if t else None}}


async def quiz(ctx: TaskContext) -> dict:
    subject = " ".join(str(ctx.args.get("subject") or "").split())[:200]
    if not subject:
        raise KeplerError("What should I quiz you on?")
    count = max(3, min(10, int(ctx.args.get("count") or rules()["quiz_questions"])))
    if ctx.args.get("topic_id") is not None:
        t = await store.topic_by_id(app.state.db, ctx.user, int(ctx.args["topic_id"]))
        points = [p for r in (await store.research_in(app.state.db, t["id"], 6) if t else []) for p in r["key_points"]]
    else:
        t, points = await _grounding(ctx, subject)
    learning = await store.learning(app.state.db, ctx.user, subject)
    qs = R.read_quiz(await ctx.orch.llm(R.QUIZ, f"Subject: {subject}\nQuestions: {count}\n\n{R.level_note(learning)}\n\n"
                                                 f"{R.grounding(t, points)}"), count)
    return {"headline": f"{len(qs)} questions on {subject}",
            "quiz": {"subject": subject, "topic_id": t["id"] if t else None, "questions": qs}}


async def read_pdf(ctx: TaskContext) -> dict:
    url, name = str(ctx.args.get("url") or "").strip(), str(ctx.args.get("file") or "").strip()
    if url:
        await ctx.progress(0, 1, "Reading the PDF")
        page = await ctx.orch.run("nebula.read", {"url": url}, lambda: ctx.cancelled, 60)
        return {k: page[k] for k in ("title", "url", "site", "words", "text", "truncated", "pages") if k in page}
    if not name:
        raise KeplerError("Give me a link or the name of a document in your downloads or file server.")
    await ctx.progress(0, 1, "Reading the document")
    path, place, rel = await asyncio.to_thread(documents.find, name, [("your downloads", settings.downloads_dir),
                                                                      ("your file server", settings.files_dir)])
    text, pages = await asyncio.to_thread(documents.read, path)
    out = {"title": os.path.basename(path), "url": "", "site": place.removeprefix("your "), "path": rel,
           "words": len(text.split()), "text": text[:12000] + ("…" if len(text) > 12000 else ""),
           "truncated": len(text) > 12000}
    if pages:
        out["pages"] = pages
    return out


async def _drop_note(pool, user: str, t: Trilium, note_id: str) -> str:
    """Deletes a Trilium note; never fails the forget. If Trilium can't be reached, the note waits in a
    queue and the catch-up round removes it later."""
    if not t.ready or not rules()["trilium"].get("enabled", True):
        await store.queue_delete(pool, user, note_id)         # removed once Trilium is set up or back on
        return "queued"
    try:
        async with _sync_lock:
            await t.delete(note_id)
        return "saved"
    except httpx.HTTPError as exc:
        log.warning("couldn't delete Trilium note %s (will retry): %s", note_id, exc)
        await store.queue_delete(pool, user, note_id)
        return "queued"


def _note_line(note: str) -> str:
    return {"saved": " Trilium is updated too.",
            "queued": " Trilium isn't reachable right now; I'll remove its note there once it's back.",
            "failed": " Trilium isn't reachable right now; I'll update it once it's back."}.get(note, "")


async def forget(ctx: TaskContext) -> dict:
    pool, t = app.state.db, trilium()
    if ctx.args.get("research_id") is not None:
        question, topic_id, gone = await store.delete_research(pool, ctx.user, int(ctx.args["research_id"]))
        if gone and gone["trilium_note_id"]:
            note = await _drop_note(pool, ctx.user, t, gone["trilium_note_id"])
        elif topic_id and not gone:
            note = await sync_note(pool, ctx.user, topic_id)           # the topic's note no longer lists it
        else:
            note = "off"
        return {"forgot": question, "message": "Removed from your library." + _note_line(note)}
    topic_id = ctx.args.get("topic_id")
    if topic_id is None and str(ctx.args.get("topic") or "").strip():
        found, names = await store.topic_by_name(pool, ctx.user, str(ctx.args["topic"]))
        if not found:
            raise KeplerError(f"Which topic? \"{ctx.args['topic']}\" could be {', '.join(names[:10])}." if names and
                              all(R.words(str(ctx.args['topic'])) <= R.words(n) for n in names) and len(names) > 1 else
                              f"I don't have a topic called \"{ctx.args['topic']}\"."
                              + (f" Your topics: {', '.join(names[:10])}." if names else " Your library is empty."))
        topic_id = found["id"]
    if topic_id is not None:
        gone = await store.delete_topic(pool, ctx.user, int(topic_id))
        note = await _drop_note(pool, ctx.user, t, gone["trilium_note_id"]) if gone["trilium_note_id"] else "off"
        return {"forgot": gone["name"], "message": "The topic and its research are gone from your library." + _note_line(note)}
    raise KeplerError("Say which research or topic to forget.")


TOOLS: dict[str, Callable[[TaskContext], Awaitable[dict]]] = {
    "research": research, "recall": recall, "topic": show_topic, "explain": explain, "quiz": quiz,
    "read_pdf": read_pdf, "forget": forget}


# ---- lifecycle and contract -----------------------------------------------------------------

async def heartbeat() -> None:
    global POLICY
    async with httpx.AsyncClient(timeout=5) as c:
        while True:
            try:
                await c.post(f"{settings.orchestrator_url}/registry/functions", json=REGISTRATION)
                r = await c.get(f"{settings.orchestrator_url}/governance/functions/kepler")
                if r.is_success:
                    POLICY = r.json()
            except httpx.HTTPError as exc:
                log.warning("registration failed, will retry: %s", exc)
            await asyncio.sleep(settings.heartbeat_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
    beat = asyncio.create_task(heartbeat())
    sync = asyncio.create_task(catch_up_loop())
    yield
    beat.cancel()
    sync.cancel()
    await app.state.db.close()


app = FastAPI(title="Aurelia function: Kepler", lifespan=lifespan)


class Invoke(BaseModel):
    task_id: str
    args: dict


@app.get("/health")
async def health():
    try:
        c = await store.counts(app.state.db)
    except asyncpg.UndefinedTableError:
        return {"status": "error", "database": "library tables missing (the orchestrator's migrations haven't run)"}
    except Exception as exc:
        return {"status": "error", "database": f"unreachable ({exc.__class__.__name__})"}
    out = {"status": "ok", "topics": c["topics"], "research": c["research"],
           "trilium": "connected" if trilium().ready else "not set up"}
    if c["pending"] and trilium().ready:
        out["trilium"] = f"{c['pending']} waiting to sync"
    return out


@app.post("/trilium/sync")
async def trilium_sync():
    """Catch Trilium up now instead of waiting for the next round."""
    return await catch_up(app.state.db)


@app.post("/tools/{tool}/invoke", status_code=202)
async def invoke(tool: str, body: Invoke):
    if tool not in TOOLS:
        raise HTTPException(404, f"kepler has no tool '{tool}'")
    asyncio.create_task(run(TOOLS[tool], body.task_id, body.args))
    return {"accepted": body.task_id}


@app.post("/cancel/{task_id}", status_code=202)
async def cancel(task_id: str):
    _cancelled.add(task_id)
    return {"cancelling": task_id}


async def run(tool: Callable[[TaskContext], Awaitable[dict]], task_id: str, args: dict) -> None:
    async with httpx.AsyncClient(timeout=15) as http:
        try:
            row = (await http.get(f"{settings.orchestrator_url}/tasks/{task_id}")).json()
            user = row.get("user_id") or "owner"
        except (httpx.HTTPError, ValueError):
            user = "owner"
        ctx = TaskContext(task_id, args, http, user)
        try:
            payload = {"status": "completed", "data": await tool(ctx)}
        except asyncio.CancelledError:
            payload = {"status": "cancelled", "partial": True, "data": {"cancelled": True, "message": "Stopped."}}
        except KeplerError as exc:
            payload = {"status": "failed", "error": str(exc)}
        except Exception as exc:
            log.exception("task %s failed", task_id)
            payload = {"status": "failed", "error": f"Something went wrong: {exc}"}
        finally:
            _cancelled.discard(task_id)
        try:
            await http.post(f"{settings.orchestrator_url}/tasks/{task_id}/result", json=payload)
        except httpx.HTTPError:
            log.error("couldn't report result for %s", task_id)
