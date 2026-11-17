"""Claude supervisor: batch review of logged interactions (fake Anthropic client, real DB)."""
import json
import os
from types import SimpleNamespace

import pytest

from engine import reviewer
from engine.database import InteractionLog, SessionLocal, record_interaction

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs a database")

USER = "reviewer-test-user"

ASK_RECORD = {"answer": "Because of heresy.", "justification": "Passage 2.", "status": "answered",
              "citations": [{"chunk_id": 2, "quote": "spread heresy"}], "general_knowledge": None,
              "verification": "checked", "revisions": 0, "problems": [],
              "passages": [{"chunk_id": 2, "text": "Printed books spread heresy."}],
              "models": {"answer": "gpt-oss-120b", "check": "qwen"}, "steps": []}
RUN_RECORD = {"goal": "Study", "brief": {"count": 3}, "plan": [{"name": "summarize"}], "steps": [],
              "output": {"summary": "S"}, "source": "Chapter text.", "models": {"steps": "gpt-oss-120b"}}

GOOD = {"verdict": "good", "correct": "yes", "grounded": "yes", "followed_request": "yes",
        "checker_accurate": "yes", "issues": [],
        "advice": [{"role": "answerer", "lesson": "Lead with the direct answer."}], "summary": "Fine."}
BAD = {"verdict": "major_issues", "correct": "no", "grounded": "partly", "followed_request": "no",
       "checker_accurate": "no", "issues": [{"role": "checker", "problem": "Passed an unsupported claim."}],
       "advice": [], "summary": "Wrong."}


@pytest.fixture(autouse=True)
def clean():
    yield
    db = SessionLocal()
    try:
        db.query(InteractionLog).filter_by(user_id=USER).delete()
        db.commit()
    finally:
        db.close()


def _log(kind="ask", request="Why were books banned?", record=ASK_RECORD):
    return record_interaction(user_id=USER, kind=kind, request=request, record=record, document_id=7)


def _get(log_id):
    db = SessionLocal()
    try:
        return db.get(InteractionLog, log_id)
    finally:
        db.close()


def _succeeded(log_id, review, stop_reason="end_turn"):
    msg = SimpleNamespace(stop_reason=stop_reason, model="claude-opus-5-5",
                          content=[SimpleNamespace(type="thinking"),
                                   SimpleNamespace(type="text", text=json.dumps(review))])
    return SimpleNamespace(custom_id=f"log-{log_id}", result=SimpleNamespace(type="succeeded", message=msg))


def _failed(log_id, kind, error_type=None):
    return SimpleNamespace(custom_id=f"log-{log_id}",
                           result=SimpleNamespace(type=kind, error=SimpleNamespace(type=error_type)))


class FakeBatches:
    def __init__(self, status="ended", results=()):
        self.status, self.results_list, self.created = status, list(results), []

    def create(self, requests):
        self.created.append(requests)
        return SimpleNamespace(id=f"batch-{len(self.created)}")

    def retrieve(self, batch_id):
        return SimpleNamespace(processing_status=self.status)

    def results(self, batch_id):
        return iter(self.results_list)


def _client(batches):
    return SimpleNamespace(messages=SimpleNamespace(batches=batches))


def _only_mine(fn):
    """Run submit/collect with the BATCH_LIMIT scoped to this test's rows."""
    db = SessionLocal()
    try:
        db.query(InteractionLog).filter(InteractionLog.user_id != USER,
                                        InteractionLog.review_status == "pending") \
          .update({"review_status": "parked"})
        db.commit()
        return fn()
    finally:
        db.query(InteractionLog).filter_by(review_status="parked").update({"review_status": "pending"})
        db.commit()
        db.close()


# ── requests ──────────────────────────────────────────────────────────────────

def test_ask_request_includes_question_passages_answer_and_schema():
    req = reviewer.build_request(_get(_log()))
    params = req["params"]
    content = params["messages"][0]["content"]
    assert req["custom_id"].startswith("log-")
    assert params["model"] == reviewer.REVIEW_MODEL
    assert params["output_config"]["format"]["schema"] == reviewer.REVIEW_SCHEMA
    assert "Why were books banned?" in content and "[chunk_id=2]" in content
    assert "Because of heresy." in content and '"verification": "checked"' in content
    assert "teacher" in params["system"]


def test_run_request_includes_plan_output_and_source():
    content = reviewer.build_request(_get(_log("run", "3 questions", RUN_RECORD)))["params"]["messages"][0]["content"]
    assert "3 questions" in content and '"count": 3' in content
    assert "Chapter text." in content and '"summary": "S"' in content


# ── submit / collect ──────────────────────────────────────────────────────────

def test_submit_batches_pending_logs_once():
    ids = [_log(), _log("run", "r", RUN_RECORD)]
    batches = FakeBatches()
    batch_id = _only_mine(lambda: reviewer.submit(_client(batches)))

    assert batch_id == "batch-1" and len(batches.created[0]) == 2
    assert all(_get(i).review_status == "submitted" and _get(i).review_batch == "batch-1" for i in ids)
    assert _only_mine(lambda: reviewer.submit(_client(batches))) is None


def test_collect_stores_reviews_and_handles_every_outcome():
    good, refused, invalid, expired, missing = (_log() for _ in range(5))
    _only_mine(lambda: reviewer.submit(_client(FakeBatches())))
    results = [_succeeded(good, GOOD), _succeeded(refused, GOOD, stop_reason="refusal"),
               _failed(invalid, "errored", "invalid_request"), _failed(expired, "expired")]

    assert reviewer.collect(_client(FakeBatches(results=results))) >= 4

    assert _get(good).review_status == "reviewed" and json.loads(_get(good).review_json) == GOOD
    assert _get(good).review_model == "claude-opus-5-5"
    assert _get(refused).review_status == "failed"
    assert _get(invalid).review_status == "failed"
    assert _get(expired).review_status == "pending"   # retried in a later batch
    assert _get(missing).review_status == "pending"   # not returned by the batch


def test_collect_leaves_unfinished_batches_alone():
    log_id = _log()
    _only_mine(lambda: reviewer.submit(_client(FakeBatches())))
    reviewer.collect(_client(FakeBatches(status="in_progress", results=[_succeeded(log_id, GOOD)])))
    assert _get(log_id).review_status == "submitted"


def test_run_skips_without_an_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert reviewer.run() == {"skipped": True}


# ── summary ───────────────────────────────────────────────────────────────────

def test_summary_reports_rates_problems_and_advice():
    a, b = _log(), _log()
    _only_mine(lambda: reviewer.submit(_client(FakeBatches())))
    reviewer.collect(_client(FakeBatches(results=[_succeeded(a, GOOD), _succeeded(b, BAD)])))
    _log("run", "r", RUN_RECORD)   # pending

    summary = reviewer.summarize(USER)
    group = summary["by_kind_and_model"]["ask:gpt-oss-120b"]
    assert summary["logs"] == 3 and summary["status"] == {"reviewed": 2, "pending": 1}
    assert group["reviewed"] == 2 and group["verdicts"] == {"good": 1, "major_issues": 1}
    assert group["rates"]["correct"] == 0.5 and group["rates"]["checker_accurate"] == 0.5
    assert summary["issues_by_role"] == {"checker": 1}
    assert summary["recent_advice"][0]["lesson"] == "Lead with the direct answer."
