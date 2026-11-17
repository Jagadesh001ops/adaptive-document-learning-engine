"""Supervisor reviews of logged interactions.

Reviews are written by Claude (Claude Code, via the /review-logs command), which fetches
pending logs from the admin API, reviews them, and posts each review back here.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import List, Literal

from pydantic import BaseModel, ConfigDict

from .database import InteractionLog, SessionLocal

Role = Literal["planner", "worker", "answerer", "checker", "reviser", "retrieval"]
Rating = Literal["yes", "partly", "no"]
RATED = ("correct", "grounded", "followed_request", "checker_accurate")


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role
    problem: str


class Lesson(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role
    lesson: str


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["good", "minor_issues", "major_issues"]
    correct: Rating
    grounded: Rating
    followed_request: Rating
    checker_accurate: Literal["yes", "no", "not_applicable"]
    issues: List[Issue] = []
    advice: List[Lesson] = []
    summary: str


def pending_logs(limit: int) -> list[dict]:
    db = SessionLocal()
    try:
        logs = (db.query(InteractionLog).filter_by(review_status="pending")
                .order_by(InteractionLog.id).limit(limit).all())
        return [{"id": log.id, "kind": log.kind, "created_at": log.created_at,
                 "document_id": log.document_id, "request": log.request,
                 "record": json.loads(log.record_json)} for log in logs]
    finally:
        db.close()


def count_pending() -> int:
    db = SessionLocal()
    try:
        return db.query(InteractionLog).filter_by(review_status="pending").count()
    finally:
        db.close()


def save_review(log_id: int, review: Review, reviewer: str) -> bool:
    """Store a review. Returns False if the log doesn't exist."""
    db = SessionLocal()
    try:
        log = db.get(InteractionLog, log_id)
        if log is None:
            return False
        log.review_json = review.model_dump_json()
        log.review_status, log.review_model = "reviewed", reviewer
        log.reviewed_at = datetime.now(UTC).replace(tzinfo=None)
        db.commit()
        return True
    finally:
        db.close()


def _model_of(log: InteractionLog, record: dict) -> str:
    models = record.get("models") or {}
    return models.get("answer" if log.kind == "ask" else "steps") or "unknown"


def summarize() -> dict:
    """Aggregate reviews: rates per interaction kind and model, problems by role, recent advice."""
    db = SessionLocal()
    try:
        logs = db.query(InteractionLog).order_by(InteractionLog.id.desc()).all()
    finally:
        db.close()

    groups: dict = defaultdict(lambda: {"reviewed": 0, "verdicts": Counter(), "yes": Counter()})
    issues_by_role, advice = Counter(), []
    for log in logs:
        if log.review_status != "reviewed":
            continue
        review, record = json.loads(log.review_json), json.loads(log.record_json)
        g = groups[f"{log.kind}:{_model_of(log, record)}"]
        g["reviewed"] += 1
        g["verdicts"][review["verdict"]] += 1
        g["yes"].update(f for f in RATED if review.get(f) == "yes")
        issues_by_role.update(i["role"] for i in review.get("issues", []))
        advice.extend({"role": a["role"], "lesson": a["lesson"], "log_id": log.id}
                      for a in review.get("advice", []))

    return {
        "logs": len(logs),
        "status": dict(Counter(log.review_status for log in logs)),
        "by_kind_and_model": {
            key: {"reviewed": g["reviewed"], "verdicts": dict(g["verdicts"]),
                  "rates": {f: round(g["yes"][f] / g["reviewed"], 2) for f in RATED}}
            for key, g in groups.items()
        },
        "issues_by_role": dict(issues_by_role),
        "recent_advice": advice[:30],
    }
