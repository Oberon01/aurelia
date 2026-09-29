"""What Kentrell learns: quiz results, and what they mean for his profile.

The quiz panel reports his score here. A strong result (80% or better on four
or more questions) becomes an "About you" proposal in the mailbox ("Solid on
Azure identity basics"), in the "learning" part of his profile. Like
everything else about him, it's kept only if he approves.
"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import db, profile

router = APIRouter(prefix="/learning", tags=["learning"])
STRONG = 0.8
MIN_QUESTIONS = 4


class QuizResult(BaseModel):
    subject: str
    score: int
    total: int
    topic_id: int | None = None
    missed: list[str] = []
    user: str = "owner"


@router.post("/quiz")
async def quiz_result(body: QuizResult, request: Request):
    from . import mailbox, tasks
    subject = " ".join(body.subject.split())[:120]
    if not subject or body.total < 1 or not 0 <= body.score <= body.total:
        raise HTTPException(400, "subject, score and total don't add up")
    await db.pool().execute(
        "INSERT INTO quiz_results (user_id, subject, topic_id, score, total, missed) VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
        body.user, subject, body.topic_id, body.score, body.total, json.dumps(body.missed[:20]))
    proposed = False
    if body.total >= MIN_QUESTIONS and body.score / body.total >= STRONG:
        when = datetime.now(timezone.utc).strftime("%b %-d")
        fact = f"Solid on {subject} (quiz {body.score}/{body.total}, {when})"
        out = await mailbox.propose(mailbox.Proposal(
            kind="about_you", fingerprint=profile.fingerprint(f"solid on {subject}"), source="kepler",
            title=fact, summary="From a quiz you took.", evidence=[f"Scored {body.score} of {body.total}."],
            proposal={"fact": fact, "category": "learning", "core": False}, strength=1, confidence=body.score / body.total,
            effect="I'll remember what you know, and teach from there next time."), tasks._AppContext(request.app))
        proposed = out.get("result") == "created"
    return {"recorded": True, "proposed": proposed}
