"""Supervisor reviews: admin API for fetching logs and saving Claude's reviews."""
import os

import pytest
from fastapi.testclient import TestClient

from api import app
from engine.database import InteractionLog, SessionLocal, record_interaction

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs a database")

client = TestClient(app, raise_server_exceptions=False)
TOKEN = "test-admin-token"
AUTH = {"X-Admin-Token": TOKEN}
USER = "reviews-test-user"

RECORD = {"answer": "Because of heresy.", "passages": [{"chunk_id": 2, "text": "Books spread heresy."}],
          "models": {"answer": "reviews-test-model", "check": "qwen"}, "steps": []}
GOOD = {"verdict": "good", "correct": "yes", "grounded": "yes", "followed_request": "yes",
        "checker_accurate": "yes", "issues": [],
        "advice": [{"role": "answerer", "lesson": "Lead with the direct answer."}], "summary": "Fine."}
BAD = {"verdict": "major_issues", "correct": "no", "grounded": "partly", "followed_request": "no",
       "checker_accurate": "no", "issues": [{"role": "checker", "problem": "Passed an unsupported claim."}],
       "advice": [], "summary": "Wrong."}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    yield
    db = SessionLocal()
    try:
        db.query(InteractionLog).filter_by(user_id=USER).delete()
        db.commit()
    finally:
        db.close()


def _log(request="Why were books banned?"):
    return record_interaction(user_id=USER, kind="ask", request=request, record=RECORD, document_id=7)


# ── access control ────────────────────────────────────────────────────────────

def test_admin_routes_do_not_exist_without_a_configured_token(monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN")
    assert client.get("/admin/logs", headers=AUTH).status_code == 404
    assert client.get("/admin/reviews/summary", headers=AUTH).status_code == 404


def test_admin_routes_reject_missing_or_wrong_tokens():
    assert client.get("/admin/logs").status_code == 403
    assert client.get("/admin/logs", headers={"X-Admin-Token": "nope"}).status_code == 403
    assert client.post("/admin/logs/1/review", json=GOOD, headers={"X-Admin-Token": "nope"}).status_code == 403


# ── fetching and saving ───────────────────────────────────────────────────────

def test_pending_logs_include_the_full_record():
    log_id = _log()
    body = client.get("/admin/logs", headers=AUTH).json()
    [log] = [entry for entry in body["logs"] if entry["id"] == log_id]
    assert log["kind"] == "ask" and log["request"] == "Why were books banned?"
    assert log["record"]["passages"][0]["text"] == "Books spread heresy."
    assert body["pending_total"] >= 1


def test_saving_a_review_marks_the_log_reviewed():
    log_id = _log()
    res = client.post(f"/admin/logs/{log_id}/review", json=GOOD, headers=AUTH)
    assert res.status_code == 200 and res.json()["review_status"] == "reviewed"
    assert log_id not in [entry["id"] for entry in client.get("/admin/logs", headers=AUTH).json()["logs"]]

    db = SessionLocal()
    try:
        log = db.get(InteractionLog, log_id)
        assert log.review_model == "claude-code" and log.reviewed_at is not None
    finally:
        db.close()


def test_malformed_reviews_are_rejected():
    log_id = _log()
    for bad in ({**GOOD, "verdict": "great"},
                {**GOOD, "issues": [{"role": "teacher", "problem": "x"}]},
                {**GOOD, "extra": 1},
                {k: v for k, v in GOOD.items() if k != "summary"}):
        assert client.post(f"/admin/logs/{log_id}/review", json=bad, headers=AUTH).status_code == 422
    assert client.post("/admin/logs/999999999/review", json=GOOD, headers=AUTH).status_code == 404


# ── summary ───────────────────────────────────────────────────────────────────

def test_summary_reports_rates_problems_and_advice():
    a, b = _log("Q1"), _log("Q2")
    _log("Q3")  # stays pending
    client.post(f"/admin/logs/{a}/review", json=GOOD, headers=AUTH)
    client.post(f"/admin/logs/{b}/review", json=BAD, headers=AUTH)

    summary = client.get("/admin/reviews/summary", headers=AUTH).json()
    group = summary["by_kind_and_model"]["ask:reviews-test-model"]
    assert summary["status"]["reviewed"] >= 2 and summary["status"]["pending"] >= 1
    assert group["reviewed"] == 2 and group["verdicts"] == {"good": 1, "major_issues": 1}
    assert group["rates"]["correct"] == 0.5 and group["rates"]["checker_accurate"] == 0.5
    assert summary["issues_by_role"]["checker"] >= 1
    assert {"role": "answerer", "lesson": "Lead with the direct answer.", "log_id": a} in summary["recent_advice"]


def test_not_applicable_ratings_are_left_out_of_rates():
    log_id = record_interaction(user_id=USER, kind="run", request="r", document_id=7,
                                record={"models": {"steps": "reviews-na-model"}})
    review = {**GOOD, "checker_accurate": "not_applicable", "grounded": "partly"}
    assert client.post(f"/admin/logs/{log_id}/review", json=review, headers=AUTH).status_code == 200
    rates = client.get("/admin/reviews/summary", headers=AUTH).json()["by_kind_and_model"][
        "run:reviews-na-model"]["rates"]
    assert rates["checker_accurate"] is None and rates["grounded"] == 0.0 and rates["correct"] == 1.0


def test_turn_logs_are_grouped_by_the_tools_used():
    log_id = record_interaction(user_id=USER, kind="turn", request="explain then quiz", document_id=7,
                                record={"plan": {"steps": [{"tool": "answer"}, {"tool": "quiz"}]},
                                        "models": {"steps": "reviews-turn-model"}})
    keeper_issue = {**GOOD, "issues": [{"role": "keeper", "problem": "Lost the earlier topic."}]}
    assert client.post(f"/admin/logs/{log_id}/review", json=keeper_issue, headers=AUTH).status_code == 200
    summary = client.get("/admin/reviews/summary", headers=AUTH).json()
    assert summary["by_kind_and_model"]["turn:answer+quiz:reviews-turn-model"]["reviewed"] == 1
    assert summary["issues_by_role"]["keeper"] >= 1
