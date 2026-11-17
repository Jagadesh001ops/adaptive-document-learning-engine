"""Claude reviews every logged interaction offline, through the Message Batches API.

Run on a schedule (e.g. hourly cron on the VM):
    docker exec ai-engine python -m engine.reviewer

Each run first collects results from finished batches, then submits pending logs as a
new batch. Batches cost half the normal price and usually finish within an hour.
"""
from __future__ import annotations

import json
import logging
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime

from .database import InteractionLog, SessionLocal

logger = logging.getLogger(__name__)

REVIEW_MODEL = os.getenv("REVIEW_MODEL", "claude-opus-5-5")
REVIEW_EFFORT = os.getenv("REVIEW_EFFORT", "medium")
BATCH_LIMIT = int(os.getenv("REVIEW_BATCH_LIMIT", "500"))

ROLES = ["planner", "worker", "answerer", "checker", "reviser", "retrieval"]
_RATING = {"type": "string", "enum": ["yes", "partly", "no"]}


def _role_notes(field: str) -> dict:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"role": {"type": "string", "enum": ROLES}, field: {"type": "string"}},
            "required": ["role", field],
            "additionalProperties": False,
        },
    }


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["good", "minor_issues", "major_issues"]},
        "correct": _RATING,
        "grounded": _RATING,
        "followed_request": _RATING,
        "checker_accurate": {"type": "string", "enum": ["yes", "no", "not_applicable"]},
        "issues": _role_notes("problem"),
        "advice": _role_notes("lesson"),
        "summary": {"type": "string"},
    },
    "required": ["verdict", "correct", "grounded", "followed_request", "checker_accurate",
                 "issues", "advice", "summary"],
    "additionalProperties": False,
}

SYSTEM = """You supervise a team of smaller AI models that help students study their documents.
You are reviewing one logged interaction after the fact, the way a teacher marks a student's work.

The roles in the team:
- planner: turns the user's request into steps and a brief of stated preferences
- worker: runs a step (summaries, concepts, quizzes, ...)
- answerer: answers a question from document passages, with quotes
- checker: judges whether an answer is supported by the passages
- reviser: rewrites an answer after a failed check
- retrieval: picks the document passages the others work from

Judge only against the source material in the log, not your own knowledge of the topic.
- correct: is the final output right according to the source?
- grounded: is every claim backed by the source, with no invented facts or quotes?
- followed_request: does the output deliver what the user actually asked for (intent, format,
  number of items, difficulty, focus)? If the request changed something earlier, judge whether
  the change is exactly what the user asked for.
- checker_accurate: if the log includes a checker verdict, was it right? Otherwise not_applicable.
- issues: concrete problems, each tagged with the role responsible.
- advice: lessons a teacher would give each role so it does better next time. Each lesson must be
  a general, reusable instruction, not about this particular document. Only include advice where
  something should improve; an empty list is fine.
- summary: one or two sentences on how the interaction went."""


def _ask_message(request: str, record: dict) -> str:
    passages = "\n\n".join(f"[chunk_id={p['chunk_id']}]\n{p['text']}" for p in record.get("passages", []))
    answer = {k: record.get(k) for k in ("answer", "justification", "citations", "status", "general_knowledge")}
    trace = {k: record.get(k) for k in ("verification", "revisions", "problems", "models")}
    steps = [{k: s.get(k) for k in ("name", "success", "detail", "error")} for s in record.get("steps", [])]
    return (f"Interaction type: question about a document\n\nUser's question:\n{request}\n\n"
            f"Passages the team read:\n{passages}\n\nFinal answer:\n{json.dumps(answer, indent=1)}\n\n"
            f"Self-check outcome:\n{json.dumps(trace)}\n\nSteps:\n{json.dumps(steps)}")


def _run_message(request: str, record: dict) -> str:
    plan = {k: record.get(k) for k in ("goal", "brief", "plan")}
    steps = record.get("steps", [])
    return (f"Interaction type: workflow run\n\nUser's request:\n{request}\n\n"
            f"Plan:\n{json.dumps(plan, indent=1)}\n\nStep results:\n{json.dumps(steps)}\n\n"
            f"Output:\n{json.dumps(record.get('output'), indent=1, default=str)}\n\n"
            f"Models: {json.dumps(record.get('models'))}\n\n"
            f"Source text the steps worked from:\n{record.get('source') or '(not recorded)'}")


def build_request(log: InteractionLog) -> dict:
    record = json.loads(log.record_json)
    content = _ask_message(log.request, record) if log.kind == "ask" else _run_message(log.request, record)
    return {
        "custom_id": f"log-{log.id}",
        "params": {
            "model": REVIEW_MODEL,
            "max_tokens": 16000,
            "system": SYSTEM,
            "output_config": {"effort": REVIEW_EFFORT,
                              "format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
            "messages": [{"role": "user", "content": content}],
        },
    }


def submit(client) -> str | None:
    """Send every pending log to Claude as one batch. Returns the batch id, or None if nothing to do."""
    db = SessionLocal()
    try:
        logs = (db.query(InteractionLog).filter_by(review_status="pending")
                .order_by(InteractionLog.id).limit(BATCH_LIMIT).all())
        if not logs:
            return None
        batch = client.messages.batches.create(requests=[build_request(log) for log in logs])
        for log in logs:
            log.review_status, log.review_batch = "submitted", batch.id
        db.commit()
        return batch.id
    finally:
        db.close()


def _apply_result(log: InteractionLog, result) -> None:
    outcome = result.result
    if outcome.type == "succeeded":
        msg = outcome.message
        if msg.stop_reason == "refusal":
            log.review_status, log.review_json = "failed", json.dumps({"error": "review refused"})
            return
        text = next((b.text for b in msg.content if b.type == "text"), "")
        try:
            log.review_json = json.dumps(json.loads(text))
        except ValueError:
            log.review_status, log.review_json = "failed", json.dumps({"error": "review was not valid JSON"})
            return
        log.review_status, log.review_model = "reviewed", msg.model
        log.reviewed_at = datetime.now(UTC).replace(tzinfo=None)
    elif outcome.type == "errored" and getattr(outcome.error, "type", None) == "invalid_request":
        log.review_status, log.review_json = "failed", json.dumps({"error": "invalid review request"})
    else:  # server error, canceled or expired: try again in a later batch
        log.review_status, log.review_batch = "pending", None


def collect(client) -> int:
    """Store reviews from finished batches. Returns how many logs were updated."""
    db = SessionLocal()
    updated = 0
    try:
        batch_ids = [b for (b,) in db.query(InteractionLog.review_batch)
                     .filter_by(review_status="submitted").distinct()]
        for batch_id in batch_ids:
            if client.messages.batches.retrieve(batch_id).processing_status != "ended":
                continue
            for result in client.messages.batches.results(batch_id):
                log = db.get(InteractionLog, int(result.custom_id.removeprefix("log-")))
                if log is not None and log.review_status == "submitted":
                    _apply_result(log, result)
                    updated += 1
            # Anything the batch didn't return goes back into the queue.
            (db.query(InteractionLog).filter_by(review_batch=batch_id, review_status="submitted")
             .update({"review_status": "pending", "review_batch": None}))
        db.commit()
        return updated
    finally:
        db.close()


def run(client=None) -> dict:
    if client is None:
        if not os.getenv("ANTHROPIC_API_KEY"):
            logger.info("ANTHROPIC_API_KEY not set; skipping reviews")
            return {"skipped": True}
        import anthropic
        client = anthropic.Anthropic()
    collected = collect(client)
    batch_id = submit(client)
    return {"collected": collected, "submitted_batch": batch_id}


def _model_of(log: InteractionLog, record: dict) -> str:
    models = record.get("models") or {}
    return models.get("answer") if log.kind == "ask" else models.get("steps", "unknown")


def summarize(user_id: str | None) -> dict:
    """Aggregate reviews: rates per interaction kind and model, problems by role, recent advice."""
    db = SessionLocal()
    try:
        logs = db.query(InteractionLog).filter_by(user_id=user_id).order_by(InteractionLog.id.desc()).all()
    finally:
        db.close()

    status = Counter(log.review_status for log in logs)
    groups: dict = defaultdict(lambda: {"reviewed": 0, "verdicts": Counter(), "yes": Counter()})
    issues_by_role, advice = Counter(), []
    for log in logs:
        if log.review_status != "reviewed":
            continue
        review, record = json.loads(log.review_json), json.loads(log.record_json)
        g = groups[f"{log.kind}:{_model_of(log, record)}"]
        g["reviewed"] += 1
        g["verdicts"][review["verdict"]] += 1
        for field in ("correct", "grounded", "followed_request", "checker_accurate"):
            if review.get(field) == "yes":
                g["yes"][field] += 1
        issues_by_role.update(i["role"] for i in review.get("issues", []))
        advice.extend({"role": a["role"], "lesson": a["lesson"], "log_id": log.id}
                      for a in review.get("advice", []))

    return {
        "logs": len(logs),
        "status": dict(status),
        "by_kind_and_model": {
            key: {"reviewed": g["reviewed"], "verdicts": dict(g["verdicts"]),
                  "rates": {f: round(g["yes"][f] / g["reviewed"], 2)
                            for f in ("correct", "grounded", "followed_request", "checker_accurate")}}
            for key, g in groups.items()
        },
        "issues_by_role": dict(issues_by_role),
        "recent_advice": advice[:20],
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(run()))
