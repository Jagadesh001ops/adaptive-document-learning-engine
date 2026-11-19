"""
Integration tests — TestClient + live database, no DB mocking.

What's covered:
  - Document lifecycle: upload → list → get → delete
  - Duplicate detection: same file twice → same document_id, created=False
  - Quota enforcement: exceed MAX_DOCS_PER_USER → 429
  - Run lifecycle: POST /run (LLM mocked) → persisted → GET /runs/{id}
  - Quiz lifecycle: POST /quiz/attempt → score → GET /quiz/history
  - Export stubs: GET /runs/{id}/export/pdf and /anki return bytes for valid run
  - Search empty: GET /search returns [] when no docs exist

Runs against whatever DATABASE_URL is configured (Postgres in CI, local
Postgres dev env).  Skipped when DATABASE_URL is not set (developer hasn't
wired a local DB yet).

Mocked helpers (not what we're testing here):
  - LLM: parse_intent, build_node_for_step — no real API calls
  - Redis: api._enqueue — instant fail so sync fallback runs; embed_batch
    returns [] so sentence-transformers model is never loaded
  - Exports: generate_pdf/generate_anki return dummy bytes (export.py has
    its own unit tests in test_export.py)
"""
import io
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api import app
from engine.database import Base, Document, QuizAttempt, SessionLocal, WorkflowRun
from engine.database import engine as _db_engine

# ── Skip all tests in this module when no real DB is configured ───────────────
pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — integration tests require a real database",
)

# ── Shared TestClient ─────────────────────────────────────────────────────────
client = TestClient(app, raise_server_exceptions=False)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def create_tables():
    """Ensure all tables exist and patch infrastructure before any test runs."""
    Base.metadata.create_all(_db_engine)
    # Patch Redis enqueue (instant fail → sync fallback) + embed_batch (skip
    # sentence-transformers model load) for the entire module.
    with (
        patch("api._enqueue", side_effect=Exception("no redis in tests")),
        patch("engine.embeddings.embed_batch", return_value=[]),
    ):
        yield


@pytest.fixture(autouse=True)
def clean_tables():
    """Wipe test data between tests so each is independent."""
    yield
    db = SessionLocal()
    try:
        from engine.database import InteractionLog, StudyMessage, StudySession
        session_ids = [s for (s,) in db.query(StudySession.id).filter_by(user_id="dev-user")]
        if session_ids:
            db.query(StudyMessage).filter(StudyMessage.session_id.in_(session_ids)).delete()
            db.query(StudySession).filter(StudySession.id.in_(session_ids)).delete()
        db.query(InteractionLog).filter_by(user_id="dev-user").delete()
        db.query(QuizAttempt).filter_by(user_id="dev-user").delete()
        db.query(WorkflowRun).filter_by(user_id="dev-user").delete()
        db.query(Document).filter_by(user_id="dev-user").delete()
        db.commit()
    finally:
        db.close()


def _txt_file(name: str = "sample.txt", content: str = "Hello, integration test.") -> dict:
    """Build a multipart file dict for TestClient."""
    return {"file": (name, io.BytesIO(content.encode()), "text/plain")}


def _make_plan():
    from engine.schemas import WorkflowPlan, WorkflowStep
    step = WorkflowStep(
        name="summarize",
        description="Summarise",
        input_key="document",
        output_key="summary",
    )
    return WorkflowPlan(
        goal="Summarise",
        scope="single_document",
        steps=[step],
        estimated_steps=1,
        too_broad=False,
    )


def _fake_node():
    node = MagicMock()
    node.execute.return_value = MagicMock(
        success=True, output_key="summary", output_value="Integration summary.",
        input_tokens=5, output_tokens=10, execution_time_ms=20, error=None,
    )
    return node


def _post_run():
    """Helper: POST /run with LLM mocked, return parsed JSON body."""
    with (
        patch("api.parse_intent", return_value=_make_plan()),
        patch("api.build_node_for_step", return_value=_fake_node()),
    ):
        res = client.post("/run", json={"document": "Some text.", "request": "summarise"})
    assert res.status_code == 200, res.text
    return res.json()


# ── Document lifecycle ────────────────────────────────────────────────────────

def test_upload_creates_document():
    res = client.post("/documents", files=_txt_file())
    assert res.status_code == 202
    body = res.json()
    assert "document_id" in body
    assert isinstance(body["document_id"], int)
    assert body["filename"] == "sample.txt"
    assert body["created"] is True
    assert body["quota_used"] == 1


def test_list_documents_includes_upload():
    client.post("/documents", files=_txt_file())
    res = client.get("/documents")
    assert res.status_code == 200
    docs = res.json()
    assert len(docs) == 1
    assert docs[0]["filename"] == "sample.txt"


def test_get_document_by_id():
    upload = client.post("/documents", files=_txt_file())
    doc_id = upload.json()["document_id"]
    res = client.get(f"/documents/{doc_id}")
    assert res.status_code == 200
    assert res.json()["document_id"] == doc_id


def test_get_document_not_found():
    res = client.get("/documents/999999")
    assert res.status_code == 404


def test_delete_document():
    upload = client.post("/documents", files=_txt_file())
    doc_id = upload.json()["document_id"]

    del_res = client.delete(f"/documents/{doc_id}")
    assert del_res.status_code == 204

    get_res = client.get(f"/documents/{doc_id}")
    assert get_res.status_code == 404


def test_delete_nonexistent_document():
    res = client.delete("/documents/999999")
    assert res.status_code == 404


def test_list_empty_after_delete():
    upload = client.post("/documents", files=_txt_file())
    doc_id = upload.json()["document_id"]
    client.delete(f"/documents/{doc_id}")
    res = client.get("/documents")
    assert res.json() == []


# ── Duplicate detection ───────────────────────────────────────────────────────

def test_duplicate_upload_returns_same_id():
    r1 = client.post("/documents", files=_txt_file(content="Unique content abc123."))
    r2 = client.post("/documents", files=_txt_file(content="Unique content abc123."))
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r1.json()["document_id"] == r2.json()["document_id"]
    assert r2.json()["created"] is False


def test_different_content_different_id():
    r1 = client.post("/documents", files=_txt_file(content="Content A"))
    r2 = client.post("/documents", files=_txt_file("b.txt", content="Content B"))
    assert r1.json()["document_id"] != r2.json()["document_id"]


# ── Quota enforcement ─────────────────────────────────────────────────────────

def test_quota_exceeded_returns_429():
    """Upload MAX_DOCS_PER_USER docs, then confirm the 11th is rejected."""
    from engine.database import MAX_DOCS_PER_USER
    for i in range(MAX_DOCS_PER_USER):
        res = client.post("/documents", files=_txt_file(f"doc{i}.txt", f"quota filler {i}"))
        assert res.status_code == 202, f"upload {i} failed: {res.text}"

    res = client.post("/documents", files=_txt_file("overflow.txt", "this should be blocked"))
    assert res.status_code == 429
    assert "quota" in res.json().get("detail", "").lower()


# ── Run lifecycle ─────────────────────────────────────────────────────────────

def test_run_persisted_in_db():
    body = _post_run()
    run_id = body["run_id"]

    db = SessionLocal()
    try:
        run = db.query(WorkflowRun).filter_by(id=run_id).first()
        assert run is not None
        assert run.user_id == "dev-user"
    finally:
        db.close()


def test_get_run_by_id_returns_correct_data():
    run_id = _post_run()["run_id"]
    res = client.get(f"/runs/{run_id}")
    assert res.status_code == 200
    assert res.json()["run_id"] == run_id


def test_runs_list_includes_new_run():
    run_id = _post_run()["run_id"]
    res = client.get("/runs")
    assert res.status_code == 200
    ids = [r["id"] for r in res.json()]   # list endpoint uses "id", detail uses "run_id"
    assert run_id in ids


def test_multiple_runs_all_in_list():
    id1 = _post_run()["run_id"]
    id2 = _post_run()["run_id"]
    res = client.get("/runs")
    ids = [r["id"] for r in res.json()]
    assert id1 in ids
    assert id2 in ids


# ── Quiz lifecycle ────────────────────────────────────────────────────────────

def test_quiz_attempt_score_returned():
    run_id = _post_run()["run_id"]
    res = client.post(
        "/quiz/attempt",
        json={
            "run_id": run_id,
            "answers": [
                {"question_idx": 0, "question_text": "Q1?",
                 "chosen_answer": "A", "correct_answer": "A"},
                {"question_idx": 1, "question_text": "Q2?",
                 "chosen_answer": "B", "correct_answer": "C"},
            ],
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["score"] == 1
    assert body["total"] == 2
    assert body["pct"] == 50


def test_quiz_attempt_persisted_in_history():
    run_id = _post_run()["run_id"]
    client.post(
        "/quiz/attempt",
        json={
            "run_id": run_id,
            "answers": [
                {"question_idx": 0, "question_text": "Q?",
                 "chosen_answer": "A", "correct_answer": "A"},
            ],
        },
    )
    hist = client.get("/quiz/history").json()
    run_ids = [h["run_id"] for h in hist]
    assert run_id in run_ids


def test_quiz_correct_answer_flag():
    run_id = _post_run()["run_id"]
    client.post(
        "/quiz/attempt",
        json={
            "run_id": run_id,
            "answers": [
                {"question_idx": 0, "question_text": "Q?",
                 "chosen_answer": "A", "correct_answer": "A"},
                {"question_idx": 1, "question_text": "Q2?",
                 "chosen_answer": "B", "correct_answer": "D"},
            ],
        },
    )
    hist = client.get("/quiz/history").json()
    for entry in hist:
        if entry["run_id"] == run_id and entry["question_idx"] == 0:
            assert entry["is_correct"] is True
        if entry["run_id"] == run_id and entry["question_idx"] == 1:
            assert entry["is_correct"] is False


# ── Exports ───────────────────────────────────────────────────────────────────
# generate_pdf/generate_anki have their own unit tests in test_export.py.
# Here we just verify the HTTP layer: valid run → 200, bad run → 404.

def test_export_pdf_returns_bytes():
    run_id = _post_run()["run_id"]
    with patch("engine.export.generate_pdf", return_value=b"%PDF-stub"):
        res = client.get(f"/runs/{run_id}/export/pdf")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/pdf"
    assert len(res.content) > 0


def test_export_anki_returns_bytes():
    run_id = _post_run()["run_id"]
    with patch("engine.export.generate_anki", return_value=b"PK\x03\x04stub"):
        res = client.get(f"/runs/{run_id}/export/anki")
    assert res.status_code == 200
    assert len(res.content) > 0


def test_export_nonexistent_run():
    with patch("engine.export.generate_pdf", return_value=b"%PDF-stub"):
        res = client.get("/runs/999999/export/pdf")
    assert res.status_code == 404


# ── Search ────────────────────────────────────────────────────────────────────

def test_search_no_docs_returns_empty():
    """With no documents in the library, search must return an empty list."""
    res = client.get("/search", params={"q": "anything"})
    assert res.status_code == 200
    assert res.json()["results"] == []


# ── Request context reaches steps ─────────────────────────────────────────────

def test_run_passes_request_context_to_steps():
    from engine.schemas import RequestBrief
    plan = _make_plan()
    plan.brief = RequestBrief(format="bullet points", count=9)   # a shared count is never passed on
    plan.steps[0].count = 3
    node = _fake_node()
    with (
        patch("api.parse_intent", return_value=plan),
        patch("api.build_node_for_step", return_value=node),
    ):
        res = client.post("/run", json={"document": "Text.", "request": "bullet summary, 3 questions"})
    assert res.status_code == 200, res.text
    ctx = node.set_context.call_args[0][0]
    assert ctx.request == "bullet summary, 3 questions"
    assert ctx.goal == "Summarise" and ctx.instructions == "Summarise"
    assert ctx.brief.preferences() == {"format": "bullet points", "count": 3}


# ── Document Q&A endpoint ─────────────────────────────────────────────────────

def _sse_events(res):
    import json as _json
    return [_json.loads(line[6:]) for line in res.text.splitlines() if line.startswith("data: ")]


def test_ask_streams_steps_and_final_answer():
    doc_id = _library_doc(chunks=[(0, 5, "Hello")])

    def fake_answer(question, document_id, emit):
        emit("step_start", {"name": "answer"})
        emit("step", {"name": "answer", "success": True, "time_ms": 5, "tokens": 9})
        return {"answer": f"About {question}", "verification": "checked", "document": document_id}

    try:
        with patch("engine.qa.answer_question", side_effect=fake_answer):
            res = client.post(f"/documents/{doc_id}/ask", json={"question": "greetings"})
        assert res.status_code == 200
        events = _sse_events(res)
        assert [e["type"] for e in events] == ["step_start", "step", "done"]
        assert events[-1]["answer"] == "About greetings" and events[-1]["document"] == doc_id
    finally:
        _cleanup_doc(doc_id)


def test_ask_reports_rate_limit_as_error_event():
    from engine.qa import ModelBusy
    doc_id = _library_doc(chunks=[(0, 5, "Hello")])
    try:
        with patch("engine.qa.answer_question", side_effect=ModelBusy("The model is busy")):
            events = _sse_events(client.post(f"/documents/{doc_id}/ask", json={"question": "q"}))
        assert events == [{"type": "error", "message": "The model is busy"}]
    finally:
        _cleanup_doc(doc_id)


def test_ask_rejects_other_users_documents_and_unready_ones():
    other = _library_doc(user_id="someone-else", chunks=[(0, 5, "Hello")])
    try:
        assert client.post(f"/documents/{other}/ask", json={"question": "q"}).status_code == 404
    finally:
        _cleanup_doc(other)

    res = client.post("/documents", files=_txt_file("unready.txt", "Processing later."))
    doc_id = res.json()["document_id"]
    db = SessionLocal()
    try:
        db.query(Document).filter_by(id=doc_id).update({"status": "processing"})
        db.commit()
    finally:
        db.close()
    assert client.post(f"/documents/{doc_id}/ask", json={"question": "q"}).status_code == 409
    assert client.post(f"/documents/{doc_id}/ask", json={"question": ""}).status_code == 422


# ── Interactions are logged for review ────────────────────────────────────────

def _logs(kind):
    import json as _json

    from engine.database import InteractionLog
    db = SessionLocal()
    try:
        return [(log, _json.loads(log.record_json))
                for log in db.query(InteractionLog).filter_by(user_id="dev-user", kind=kind).all()]
    finally:
        db.close()


def test_ask_is_logged_with_passages_but_passages_are_not_sent_to_the_browser():
    doc_id = _library_doc(chunks=[(0, 5, "Hello")])
    result = {"answer": "Hi", "verification": "checked",
              "passages": [{"chunk_id": 1, "text": "Hello"}], "models": {"answer": "m1", "check": "m2"}}

    def fake_answer(question, document_id, emit):
        emit("step", {"name": "answer", "success": True})
        return result

    try:
        with patch("engine.qa.answer_question", side_effect=fake_answer):
            events = _sse_events(client.post(f"/documents/{doc_id}/ask", json={"question": "greet?"}))
        assert "passages" not in events[-1]
        [(log, record)] = _logs("ask")
        assert log.request == "greet?" and log.document_id == doc_id and log.review_status == "pending"
        assert record["passages"] == [{"chunk_id": 1, "text": "Hello"}]
        assert record["steps"] == [{"name": "answer", "success": True}]
    finally:
        _cleanup_doc(doc_id)


def test_run_is_logged_with_plan_steps_output_and_source():
    from engine.schemas import RequestBrief
    plan = _make_plan()
    plan.brief = RequestBrief(count=3)
    with (
        patch("api.parse_intent", return_value=plan),
        patch("api.build_node_for_step", return_value=_fake_node()),
    ):
        run_id = client.post("/run", json={"document": "Source text.", "request": "summarise"}).json()["run_id"]
    [(log, record)] = _logs("run")
    assert log.request == "summarise"
    assert record["run_id"] == run_id and record["brief"] == {"count": 3}
    assert record["plan"][0]["instructions"] == "Summarise"
    assert record["output"] == {"summary": "Integration summary."}
    assert record["source"] == "Source text."
    assert record["steps"][0]["success"] is True


# ── Workflow retrieval is limited to the run's document ───────────────────────

def _retrieval_plan():
    from engine.schemas import WorkflowPlan, WorkflowStep
    return WorkflowPlan(goal="Find", estimated_steps=1, steps=[
        WorkflowStep(name="retrieve_chunks", description="Find passages",
                     input_key="query", output_key="retrieved_chunks")])


def test_workflow_retrieval_searches_only_the_selected_document():
    doc_id = _library_doc(chunks=[(0, 5, "Hello")])
    try:
        with (
            patch("api.parse_intent", return_value=_retrieval_plan()),
            patch("engine.retrieval_node.retrieve_chunks", return_value=[]) as retrieve,
        ):
            res = client.post("/run", json={"document": "x", "request": "find hello", "document_id": doc_id})
        assert res.status_code == 200, res.text
        assert retrieve.call_args.kwargs["document_id"] == doc_id
    finally:
        _cleanup_doc(doc_id)


def test_workflow_retrieval_without_a_document_does_not_search_everything():
    with (
        patch("api.parse_intent", return_value=_retrieval_plan()),
        patch("engine.retrieval_node.retrieve_chunks", return_value=[]) as retrieve,
    ):
        client.post("/run", json={"document": "raw text", "request": "find hello"})
    retrieve.assert_not_called()


# ── Background processing status ──────────────────────────────────────────────

def test_upload_returns_processing_then_becomes_ready():
    res = client.post("/documents", files=_txt_file("bg.txt", "Background processing text."))
    assert res.status_code == 202
    assert res.json()["status"] == "processing"
    # TestClient runs background tasks before returning, so processing is done here.
    doc = client.get(f"/documents/{res.json()['document_id']}").json()
    assert doc["status"] == "ready"


def test_processing_error_marks_document_failed():
    with patch("engine.chunking.chunk_text", side_effect=RuntimeError("chunker exploded")):
        res = client.post("/documents", files=_txt_file("bad.txt", "Will fail to chunk."))
    doc = client.get(f"/documents/{res.json()['document_id']}").json()
    assert doc["status"] == "failed"
    assert "chunker exploded" in doc["error_message"]

    # Re-uploading the same file retries processing.
    retry = client.post("/documents", files=_txt_file("bad.txt", "Will fail to chunk."))
    assert retry.json()["document_id"] == doc["document_id"]
    assert client.get(f"/documents/{doc['document_id']}").json()["status"] == "ready"


# ── Run with a library document ───────────────────────────────────────────────

def _library_doc(user_id: str = "dev-user", chunks=()):
    from engine.database import DocumentChunk, get_or_create_document
    db = SessionLocal()
    try:
        doc, _ = get_or_create_document(
            db, filename="lib.txt", content_type="text/plain", size_bytes=10,
            page_count=1, char_count=10, content_hash=f"lib-{user_id}-{len(chunks)}",
            user_id=user_id,
        )
        for i, (start, end, text) in enumerate(chunks):
            db.add(DocumentChunk(document_id=doc.id, chunk_index=i, char_start=start,
                                 char_end=end, content_hash=f"h{doc.id}-{i}", text=text))
        db.commit()
        return doc.id
    finally:
        db.close()


def _cleanup_doc(doc_id: int):
    from engine.database import DocumentChunk
    db = SessionLocal()
    try:
        db.query(DocumentChunk).filter_by(document_id=doc_id).delete()
        db.query(Document).filter_by(id=doc_id).delete()
        db.commit()
    finally:
        db.close()


def test_run_with_document_id_uses_stored_chunks():
    # "Hello world. Goodbye." split into two chunks overlapping on "world. "
    doc_id = _library_doc(chunks=[(0, 13, "Hello world. "), (6, 21, "world. Goodbye.")])
    node = _fake_node()
    try:
        with (
            patch("api.parse_intent", return_value=_make_plan()),
            patch("api.build_node_for_step", return_value=node),
        ):
            res = client.post("/run", json={"document": "[Document: lib.txt]",
                                             "request": "summarise", "document_id": doc_id})
        assert res.status_code == 200, res.text
        state = node.execute.call_args[0][0]
        assert state["document"] == "Hello world. Goodbye."
    finally:
        _cleanup_doc(doc_id)


def test_run_with_unprocessed_document_returns_409():
    doc_id = _library_doc()
    try:
        res = client.post("/run", json={"document": "x", "request": "summarise",
                                         "document_id": doc_id})
        assert res.status_code == 409
    finally:
        _cleanup_doc(doc_id)


def test_run_with_other_users_document_returns_404():
    doc_id = _library_doc(user_id="someone-else", chunks=[(0, 5, "Hello")])
    try:
        res = client.post("/run", json={"document": "x", "request": "summarise",
                                         "document_id": doc_id})
        assert res.status_code == 404
    finally:
        _cleanup_doc(doc_id)


def test_failed_asks_are_logged_for_review():
    from engine.qa import ModelBusy
    doc_id = _library_doc(chunks=[(0, 5, "Hello")])
    try:
        with patch("engine.qa.answer_question", side_effect=ModelBusy("The model is busy")):
            _sse_events(client.post(f"/documents/{doc_id}/ask", json={"question": "why?"}))
        [(log, record)] = _logs("ask")
        assert log.request == "why?" and record["error"] == "The model is busy"
    finally:
        _cleanup_doc(doc_id)


def test_run_docx_export_returns_a_word_document():
    res = client.post("/documents", files=_txt_file("w.txt", "Words."))
    assert res.status_code == 202
    with patch("api.parse_intent", return_value=_make_plan()), \
         patch("api.build_node_for_step", return_value=_fake_node()):
        run_id = client.post("/run", json={"document": "Text.", "request": "summarise"}).json()["run_id"]
    res = client.get(f"/runs/{run_id}/export/docx")
    assert res.status_code == 200 and res.content[:2] == b"PK"
    assert res.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert client.get("/runs/999999/export/docx").status_code == 404


# ── Study sessions ────────────────────────────────────────────────────────────

def _fake_turn(message, document_id, turns, memory, emit):
    emit("step_start", {"name": "plan"})
    emit("step", {"name": "plan", "success": True, "time_ms": 3, "tokens": 5})
    block = {"type": "summary", "text": f"Summary for: {message}", "step": 0}
    emit("block", {"block": block})
    return {"message": message, "goal": "summarize", "plan": {"steps": [{"tool": "summarize"}]},
            "reply": None, "blocks": [block], "steps": [{"name": "plan", "success": True}],
            "error": None, "passages": [{"chunk_id": 1, "text": "Hello"}], "sources": {},
            "targets": [], "memory": {"chunk_ids": [len(turns) + 1]}, "total_tokens": 5, "time_ms": 3,
            "models": {"steps": "m"}, "seen_turns": len(turns), "seen_memory": memory}


def _new_session(doc_id):
    res = client.post("/sessions", json={"document_id": doc_id})
    assert res.status_code == 201, res.text
    return res.json()["session_id"]


def _send(session_id, message):
    with patch("engine.study.run_turn", side_effect=lambda **kw: _fake_turn(**kw)):
        res = client.post(f"/sessions/{session_id}/messages", json={"message": message})
    assert res.status_code == 200, res.text
    return _sse_events(res)


def test_session_conversation_streams_turns_and_remembers_them():
    doc_id = _library_doc(chunks=[(0, 5, "Hello")])
    try:
        sid = _new_session(doc_id)
        events = _send(sid, "Summarize this")
        assert [e["type"] for e in events] == ["step_start", "step", "block", "done"]
        assert events[2]["block"]["type"] == "summary"
        turn = events[-1]["turn"]
        assert turn["turn"] == 1 and turn["message"] == "Summarize this"
        assert turn["blocks"][0]["text"] == "Summary for: Summarize this"
        assert "passages" not in turn and "memory" not in turn, "review-only data stays server-side"

        second = _send(sid, "Now make it shorter")[-1]["turn"]
        assert second["turn"] == 2 and second["seen_turns"] == 1
        assert second["seen_memory"] == {"chunk_ids": [1]}, "the context keeper's memory carries over"

        session = client.get(f"/sessions/{sid}").json()
        assert session["title"] == "Summarize this"
        assert [t["message"] for t in session["turns"]] == ["Summarize this", "Now make it shorter"]
        listing = client.get("/sessions", params={"document_id": doc_id}).json()
        assert listing[0]["session_id"] == sid and listing[0]["turns"] == 2

        logs = _logs("turn")
        assert len(logs) == 2
        log, record = logs[-1]
        assert log.document_id == doc_id and record["turn"] == 2
        assert record["passages"] == [{"chunk_id": 1, "text": "Hello"}]
        assert "Turn 1 - user: Summarize this" in record["history"]
    finally:
        _cleanup_doc(doc_id)


def test_session_detail_returns_the_stored_turns_and_document():
    doc_id = _library_doc(chunks=[(0, 5, "Hello")])
    try:
        sid = _new_session(doc_id)
        _send(sid, "First")
        body = client.get(f"/sessions/{sid}").json()
        assert body["document"]["document_id"] == doc_id
        assert [t["message"] for t in body["turns"]] == ["First"]
    finally:
        _cleanup_doc(doc_id)


def test_session_export_pdf_and_docx():
    doc_id = _library_doc(chunks=[(0, 5, "Hello")])
    try:
        sid = _new_session(doc_id)
        _send(sid, "Summarize this")
        pdf = client.get(f"/sessions/{sid}/export", params={"format": "pdf"})
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
        assert 'filename="lib-study.pdf"' in pdf.headers["content-disposition"]
        docx = client.get(f"/sessions/{sid}/export", params={"format": "docx", "turn": 1,
                                                              "page_size": "letter"})
        assert docx.status_code == 200 and docx.content[:2] == b"PK"
        assert "lib-turn-1.docx" in docx.headers["content-disposition"]
        assert client.get(f"/sessions/{sid}/export", params={"turn": 5}).status_code == 404
        assert client.get(f"/sessions/{sid}/export", params={"format": "exe"}).status_code == 422
    finally:
        _cleanup_doc(doc_id)


def test_sessions_are_private_and_need_a_ready_document():
    other = _library_doc(user_id="someone-else", chunks=[(0, 5, "Hello")])
    try:
        assert client.post("/sessions", json={"document_id": other}).status_code == 404
    finally:
        _cleanup_doc(other)
    assert client.get("/sessions/999999").status_code == 404
    assert client.post("/sessions/999999/messages", json={"message": "hi"}).status_code == 404

    doc_id = _library_doc(chunks=[(0, 5, "Hello")])
    try:
        sid = _new_session(doc_id)
        assert client.post(f"/sessions/{sid}/messages", json={"message": ""}).status_code == 422
        db = SessionLocal()
        try:
            db.query(Document).filter_by(id=doc_id).update({"status": "processing"})
            db.commit()
        finally:
            db.close()
        assert client.post(f"/sessions/{sid}/messages", json={"message": "hi"}).status_code == 409
    finally:
        _cleanup_doc(doc_id)


def test_a_failed_turn_is_saved_reported_and_logged():
    doc_id = _library_doc(chunks=[(0, 5, "Hello")])

    def failing(**kw):
        result = _fake_turn(**kw)
        return {**result, "blocks": [], "error": "The model is busy"}

    try:
        sid = _new_session(doc_id)
        with patch("engine.study.run_turn", side_effect=failing):
            events = _sse_events(client.post(f"/sessions/{sid}/messages", json={"message": "quiz me"}))
        assert [e["type"] for e in events][-2:] == ["error", "done"]
        assert events[-1]["turn"]["error"] == "The model is busy"
        [(_, record)] = _logs("turn")
        assert record["error"] == "The model is busy"
    finally:
        _cleanup_doc(doc_id)


def test_deleting_a_session_or_its_document_removes_the_conversation():
    from engine.database import StudyMessage, StudySession
    res = client.post("/documents", files=_txt_file("conv.txt", "Conversation source."))
    doc_id = res.json()["document_id"]
    sid = _new_session(doc_id)
    _send(sid, "hello")
    keep = _new_session(doc_id)
    assert client.delete(f"/sessions/{sid}").status_code == 204
    assert client.get(f"/sessions/{sid}").status_code == 404

    assert client.delete(f"/documents/{doc_id}").status_code == 204
    db = SessionLocal()
    try:
        assert db.query(StudySession).filter_by(id=keep).count() == 0
        assert db.query(StudyMessage).filter(StudyMessage.session_id.in_([sid, keep])).count() == 0
    finally:
        db.close()


# ── Quiz answers and progress ─────────────────────────────────────────────────

def _quiz_turn(chunk_ids):
    def fake(message, document_id, turns, memory, emit):
        items = [{"question": f"Q{i + 1}", "options": {"A": "a", "B": "b"}, "answer": "A",
                  "citation": {"chunk_id": c, "excerpt": "x"}} for i, c in enumerate(chunk_ids)]
        block = {"type": "quiz", "items": items, "step": 0, "scope": "topic"}
        return {**_fake_turn(message, document_id, turns, memory, emit), "blocks": [block]}
    return fake


def _chunk_ids(doc_id):
    from engine.database import DocumentChunk
    db = SessionLocal()
    try:
        return [c for (c,) in db.query(DocumentChunk.id).filter_by(document_id=doc_id)
                .order_by(DocumentChunk.chunk_index)]
    finally:
        db.close()


def test_quiz_answers_are_graded_stored_and_drive_progress():
    doc_id = _library_doc(chunks=[(0, 5, "Alpha"), (5, 10, "Beta")])
    try:
        first, second = _chunk_ids(doc_id)
        with patch("engine.progress._titles", return_value=["Alpha part", "Beta part"]):
            fresh = client.get(f"/documents/{doc_id}/progress").json()
            assert [s["title"] for s in fresh["sections"]] == ["Alpha part", "Beta part"]
            assert fresh["next"]["kind"] == "overview" and fresh["latest_session_id"] is None

            sid = _new_session(doc_id)
            with patch("engine.study.run_turn", side_effect=_quiz_turn([first, second])):
                _sse_events(client.post(f"/sessions/{sid}/messages", json={"message": "quiz me"}))
            assert client.get(f"/documents/{doc_id}/progress").json()["next"]["kind"] == "finish_quiz"

            res = client.post(f"/sessions/{sid}/quiz-answers",
                              json={"turn": 1, "step": 0, "answers": {"1": "a", "2": "B"}})
            assert res.status_code == 200, res.text
            assert res.json()["correct"] == 1 and res.json()["total"] == 2

            block = client.get(f"/sessions/{sid}").json()["turns"][0]["blocks"][0]
            assert block["answers"] == {"1": "A", "2": "B"}, "a reopened quiz shows its answers"

            body = client.get(f"/documents/{doc_id}/progress").json()
        assert body["done"]["answered"] == 2 and body["done"]["correct"] == 1
        assert body["next"]["kind"] == "revisit" and body["next"]["section"] == 2
        assert body["sections"][1]["wrong"] == 1 and body["latest_session_id"] == sid

        assert client.post(f"/sessions/{sid}/quiz-answers",
                           json={"turn": 1, "step": 3, "answers": {"1": "A"}}).status_code == 404
        assert client.post(f"/sessions/{sid}/quiz-answers",
                           json={"turn": 1, "step": 0, "answers": {"7": "A"}}).status_code == 422
        assert client.delete(f"/sessions/{sid}").status_code == 204
        db = SessionLocal()
        try:
            assert db.query(QuizAttempt).filter_by(session_id=sid).count() == 0
        finally:
            db.close()
    finally:
        _cleanup_doc(doc_id)


def test_progress_needs_an_owned_ready_document():
    other = _library_doc(user_id="someone-else", chunks=[(0, 5, "Hello")])
    try:
        assert client.get(f"/documents/{other}/progress").status_code == 404
    finally:
        _cleanup_doc(other)
    doc_id = _library_doc(chunks=[(0, 5, "Hello")])
    try:
        db = SessionLocal()
        try:
            db.query(Document).filter_by(id=doc_id).update({"status": "processing"})
            db.commit()
        finally:
            db.close()
        assert client.get(f"/documents/{doc_id}/progress").status_code == 409
    finally:
        _cleanup_doc(doc_id)


def test_whole_document_text_samples_every_section_of_a_long_document():
    from engine.study import document_text
    chunks = [(i * 100, (i + 1) * 100, f"Section text number {i}. " + "words " * 12) for i in range(40)]
    doc_id = _library_doc(chunks=chunks)
    try:
        ids = _chunk_ids(doc_id)
        short, used = document_text(doc_id)
        assert used == ids and "number 39" in short, "a document that fits is sent whole"

        outline = [{"section": i + 1, "title": f"S{i + 1}", "chunk_ids": ids[i * 10:(i + 1) * 10]}
                   for i in range(4)]
        text, used = document_text(doc_id, max_chars=600, outline=outline)
        assert len(text) <= 600 + 4 * 20
        assert all(f"## S{i}" in text for i in range(1, 5)), "every section is represented"
        assert any(c in used for c in ids[30:]), "the end of the document is read, not cut off"
    finally:
        _cleanup_doc(doc_id)


def test_reuploading_a_document_from_an_older_extractor_reprocesses_it():
    from engine.database import DocumentChunk
    from engine.layout import EXTRACTOR
    content = "First sentence of the book. Second sentence follows it. " * 5
    first = client.post("/documents", files=_txt_file("old.txt", content)).json()
    doc_id = first["document_id"]
    db = SessionLocal()
    try:
        doc = db.get(Document, doc_id)
        assert doc.status == "ready" and doc.extractor == EXTRACTOR
        chunks_before = db.query(DocumentChunk).filter_by(document_id=doc_id).count()
        doc.extractor, doc.outline_json = None, '{"sections": [], "titled": true}'   # an older upload
        db.commit()
    finally:
        db.close()

    again = client.post("/documents", files=_txt_file("old.txt", content)).json()
    assert again["document_id"] == doc_id and again["created"] is False
    assert again["status"] == "processing"
    db = SessionLocal()
    try:
        doc = db.get(Document, doc_id)
        assert doc.status == "ready" and doc.extractor == EXTRACTOR and doc.outline_json is None
        assert db.query(DocumentChunk).filter_by(document_id=doc_id).count() == chunks_before, \
            "old chunks are replaced, not duplicated"
    finally:
        db.close()

    same = client.post("/documents", files=_txt_file("old.txt", content)).json()
    assert same["status"] == "ready", "an up-to-date document isn't processed again"
    client.delete(f"/documents/{doc_id}")


def test_background_processing_reorders_pdfs_with_the_model_when_enabled():
    import api
    with patch("engine.layout.model_ordering_enabled", return_value=True), \
         patch("api.ingest") as reingest, patch("api.store_chunks") as store:
        reingest.return_value.text = "Reordered text. " * 10
        reingest.return_value.char_count = 160
        reingest.return_value.layout = {"model": 1}
        api._process_document(999999, "original text", 1, 13, b"%PDF", "x.pdf", "application/pdf")
    assert reingest.call_args.kwargs["order_with_model"] is True
    stored = store.call_args.args[2]
    assert "Reordered" in stored[0].text
