from dotenv import load_dotenv

load_dotenv()

import asyncio
import contextlib
import hashlib
import json
import os
import queue
import secrets
import threading
from typing import List, Optional

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from engine.auth import get_current_user
from engine.chunking import join_chunks
from engine.database import (
    MAX_DOCS_PER_USER,
    Document,
    DocumentChunk,
    QuizAttempt,
    SessionLocal,
    StudyMessage,
    StudySession,
    WorkflowRun,
    get_or_create_document,
    init_db,
    record_interaction,
    store_chunks,
)
from engine.execution_engine import ExecutionEngine
from engine.ingestion import ExtractionError, IngestionError, ingest
from engine.intent_parser import parse_intent
from engine.llm_nodes import RetrieveChunksNode, StepContext, build_node_for_step
from engine.reviews import Review


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    # Run init_db in a background thread so the port binds immediately.
    import threading
    threading.Thread(target=init_db, daemon=True).start()
    yield


app = FastAPI(title="Adaptive Document Learning Engine", version="1.1.0", lifespan=lifespan)

# ─── Maintenance mode ─────────────────────────────────────────────────────────
# Set MAINTENANCE_MODE=1 to block all traffic with 503 (health/metrics exempt).
_MAINTENANCE = os.getenv("MAINTENANCE_MODE", "").lower() in ("1", "true", "yes")

@app.middleware("http")
async def maintenance_gate(request: Request, call_next):
    if _MAINTENANCE and request.url.path not in ("/health", "/metrics"):
        return JSONResponse(
            {"status": "maintenance", "message": "Service temporarily unavailable. Check back soon."},
            status_code=503,
        )
    return await call_next(request)

_cors_origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
_extra = os.getenv("CORS_ORIGINS", "")
_allow_all = os.getenv("CORS_ALLOW_ALL", "") == "1"
if _extra:
    _cors_origins += [o.strip() for o in _extra.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _allow_all else _cors_origins,
    allow_credentials=not _allow_all,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Prometheus metrics ───────────────────────────────────────────────────────
from prometheus_fastapi_instrumentator import Instrumentator

Instrumentator().instrument(app).expose(app, endpoint="/metrics")


# ─── DB dependency ────────────────────────────────────────────────────────────

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─── Request / Response schemas ───────────────────────────────────────────────

class RunRequest(BaseModel):
    document: str
    request: str
    document_id: Optional[int] = None   # if set, retrieval uses this doc's chunks
    difficulty: Optional[str] = "medium"  # easy | medium | hard (Day 15)

class StepResult(BaseModel):
    name: str
    input_key: str
    output_key: str

class RunResponse(BaseModel):
    run_id: int
    goal: str
    steps: list[StepResult]
    output: dict
    total_tokens: int
    total_time_ms: float

class QuizAnswerItem(BaseModel):
    question_idx: int
    question_text: str
    chosen_answer: str
    correct_answer: str

class QuizAttemptRequest(BaseModel):
    run_id: int
    answers: List[QuizAnswerItem]


# ─── Redis helper ─────────────────────────────────────────────────────────────

async def _enqueue(job_name: str, **kwargs):
    import os

    from arq.connections import RedisSettings, create_pool
    raw = os.getenv("REDIS_URL", "redis://localhost:6379")
    # Strip both redis:// and rediss:// (TLS) before parsing host:port for ARQ.
    url = raw.removeprefix("rediss://").removeprefix("redis://")
    # Upstash URLs include credentials: default:password@host:port
    if "@" in url:
        url = url.split("@", 1)[1]
    host, _, port_str = url.partition(":")
    ssl = raw.startswith("rediss://")
    settings = RedisSettings(host=host, port=int(port_str) if port_str else 6379, ssl=ssl)
    redis = await create_pool(settings)
    await redis.enqueue_job(job_name, **kwargs)
    await redis.aclose()


# ─── Documents ────────────────────────────────────────────────────────────────

def _process_document(doc_id: int, text: str, page_count: int, char_count: int) -> None:
    import logging

    from engine.chunking import chunk_text
    from engine.embeddings import embed_batch
    db = SessionLocal()
    try:
        chunks = chunk_text(text)
        embeddings = embed_batch([c.text for c in chunks]) if chunks else []
        store_chunks(db, doc_id, chunks, embeddings)
        d = db.query(Document).filter_by(id=doc_id).first()
        if d:
            d.status = "ready"
            d.page_count = page_count
            d.char_count = char_count
            d.error_message = None
            d.outline_json = None   # the chunks changed, so the section map is rebuilt
        db.commit()
    except Exception as exc:
        db.rollback()
        logging.getLogger(__name__).exception("processing document %d failed", doc_id)
        d = db.query(Document).filter_by(id=doc_id).first()
        if d:
            d.status = "failed"
            d.error_message = str(exc)
            db.commit()
    finally:
        db.close()


@app.post("/documents", status_code=202)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
):
    data = await file.read(26 * 1024 * 1024)
    try:
        # Extraction is CPU-bound; run it off the event loop so other requests keep flowing.
        result = await asyncio.to_thread(ingest, data=data, filename=file.filename or "upload",
                                         content_type=file.content_type or "")
    except IngestionError as exc:
        raise HTTPException(status_code=413 if "25 MB" in str(exc) else 415, detail=str(exc)) from exc
    except ExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    content_hash = hashlib.sha256(data).hexdigest()

    db = SessionLocal()
    try:
        doc, created = get_or_create_document(
            db, filename=result.filename, content_type=result.content_type,
            size_bytes=result.size_bytes, page_count=result.page_count,
            char_count=result.char_count, content_hash=content_hash,
            user_id=user_id,
        )
        if created:
            doc.status = "processing"
        db.commit()
        doc_id, doc_status = doc.id, doc.status
    finally:
        db.close()

    if created or doc_status in ("processing", "failed"):
        if doc_status == "failed":
            db = SessionLocal()
            try:
                db.query(Document).filter_by(id=doc_id).update({"status": "processing"})
                db.commit()
            finally:
                db.close()
        _async_ingest = os.getenv("ASYNC_INGEST", "1") == "1"
        try:
            if not _async_ingest:
                raise RuntimeError("ASYNC_INGEST=0 — forcing sync ingestion")
            await _enqueue("ingest_document", document_id=doc_id, raw_bytes=data)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Redis unavailable (%s), processing in background", exc)
            # Runs in a worker thread after the response is sent; clients poll GET /documents/{id}.
            background_tasks.add_task(_process_document, doc_id, result.text,
                                      result.page_count, result.char_count)
            doc_status = "processing"

    return {"document_id": doc_id, "status": doc_status, "created": created,
            "filename": result.filename, "size_bytes": result.size_bytes,
            "quota_used": _doc_count(user_id), "quota_max": MAX_DOCS_PER_USER}


def _doc_count(user_id: str) -> int:
    db = SessionLocal()
    try:
        return db.query(Document).filter_by(user_id=user_id).count()
    finally:
        db.close()


@app.get("/documents")
def list_documents(user_id: str = Depends(get_current_user)):
    db = SessionLocal()
    try:
        docs = (db.query(Document)
                  .filter_by(user_id=user_id)
                  .order_by(Document.created_at.desc())
                  .all())
        return [_doc_dict(d) for d in docs]
    finally:
        db.close()


@app.get("/documents/{document_id}/progress")
def document_progress(document_id: int, user_id: str = Depends(get_current_user)):
    """The document's sections, what has been studied and tested, and a suggested next task."""
    from engine.progress import document_progress as progress
    db = SessionLocal()
    try:
        doc = _owned_document(db, document_id, user_id)
        status = doc.status
    finally:
        db.close()
    if status != "ready":
        raise HTTPException(status_code=409, detail=f"Document is not ready (status: {status})")
    return progress(document_id, user_id)


@app.get("/documents/{document_id}")
def get_document(document_id: int, user_id: str = Depends(get_current_user)):
    db = SessionLocal()
    try:
        doc = db.query(Document).filter_by(id=document_id, user_id=user_id).first()
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        return _doc_dict(doc)
    finally:
        db.close()


@app.delete("/documents/{document_id}", status_code=204)
def delete_document(document_id: int, user_id: str = Depends(get_current_user)):
    db = SessionLocal()
    try:
        doc = db.query(Document).filter_by(id=document_id, user_id=user_id).first()
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        db.query(DocumentChunk).filter_by(document_id=document_id).delete()
        session_ids = [sid for (sid,) in db.query(StudySession.id).filter_by(document_id=document_id)]
        if session_ids:
            db.query(StudyMessage).filter(StudyMessage.session_id.in_(session_ids)).delete()
            db.query(QuizAttempt).filter(QuizAttempt.session_id.in_(session_ids)).delete()
            db.query(StudySession).filter(StudySession.id.in_(session_ids)).delete()
        db.delete(doc)
        db.commit()
    finally:
        db.close()


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


@app.post("/documents/{document_id}/ask")
def ask_document(document_id: int, body: AskRequest, user_id: str = Depends(get_current_user)):
    """Answer a question about one document, streaming each step as server-sent events."""
    from engine.qa import ModelBusy, answer_question

    db = SessionLocal()
    try:
        doc = db.query(Document).filter_by(id=document_id, user_id=user_id).first()
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        if doc.status != "ready":
            raise HTTPException(status_code=409, detail=f"Document is not ready (status: {doc.status})")
    finally:
        db.close()

    def event_generator():
        q: queue.Queue = queue.Queue()
        steps: list = []

        def emit(kind, payload):
            if kind == "step":
                steps.append(payload)
            q.put({"type": kind, **payload})

        def run():
            try:
                result = answer_question(body.question, document_id, emit)
                record_interaction(user_id=user_id, kind="ask", request=body.question,
                                   document_id=document_id, record={**result, "steps": steps})
                q.put({"type": "done", **{k: v for k, v in result.items() if k != "passages"}})
            except Exception as exc:
                if isinstance(exc, ModelBusy):
                    message = str(exc)
                else:
                    import logging
                    logging.getLogger(__name__).exception("ask failed for document %d", document_id)
                    message = f"Couldn't answer: {exc}"
                # Failures are logged too: they're the interactions most worth reviewing.
                record_interaction(user_id=user_id, kind="ask", request=body.question,
                                   document_id=document_id, record={"error": message, "steps": steps})
                q.put({"type": "error", "message": message})
            finally:
                q.put(None)

        threading.Thread(target=run, daemon=True).start()
        while (event := q.get()) is not None:
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})



# ─── Study sessions ───────────────────────────────────────────────────────────
# One conversation per document. Every message is planned from the same tools (answer,
# summarize, concepts, quiz, revise), so a single message can combine them and later messages
# can change earlier output. See engine/study.py.

class SessionCreate(BaseModel):
    document_id: int


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class SessionQuizAnswers(BaseModel):
    turn: int = Field(ge=1)
    step: int = Field(ge=0)
    answers: dict[int, str]     # question number (from 1) -> chosen letter


_TURN_PRIVATE = ("passages", "sources", "targets", "memory", "models", "outline")


def _owned_document(db, document_id: int, user_id: str) -> Document:
    doc = db.query(Document).filter_by(id=document_id, user_id=user_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


def _owned_session(db, session_id: int, user_id: str) -> StudySession:
    sess = db.query(StudySession).filter_by(id=session_id, user_id=user_id).first()
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    return sess


def _session_turns(db, session_id: int) -> list[dict]:
    rows = (db.query(StudyMessage).filter_by(session_id=session_id, role="assistant")
            .order_by(StudyMessage.id).all())
    return [json.loads(r.content) for r in rows]


def _attach_answers(db, session_id: int, turns: list[dict]) -> None:
    """Add each quiz block's latest answers, so a reopened session shows what was answered."""
    rows = db.query(QuizAttempt).filter_by(session_id=session_id).order_by(QuizAttempt.id).all()
    latest: dict = {}
    for a in rows:
        latest.setdefault((a.turn, a.step), {})[a.question_idx + 1] = a.chosen_answer
    for t in turns:
        for b in t.get("blocks") or []:
            if b.get("type") == "quiz" and (t.get("turn"), b.get("step")) in latest:
                b["answers"] = latest[(t.get("turn"), b.get("step"))]


def _session_dict(sess: StudySession, turns: int | None = None) -> dict:
    out = {"session_id": sess.id, "document_id": sess.document_id, "title": sess.title,
           "created_at": sess.created_at, "updated_at": sess.updated_at}
    if turns is not None:
        out["turns"] = turns
    return out


@app.post("/sessions", status_code=201)
def create_session(body: SessionCreate, user_id: str = Depends(get_current_user)):
    db = SessionLocal()
    try:
        _owned_document(db, body.document_id, user_id)
        sess = StudySession(user_id=user_id, document_id=body.document_id)
        db.add(sess)
        db.commit()
        db.refresh(sess)
        return _session_dict(sess, 0)
    finally:
        db.close()


@app.get("/sessions")
def list_sessions(document_id: Optional[int] = None, user_id: str = Depends(get_current_user)):
    db = SessionLocal()
    try:
        q = db.query(StudySession).filter_by(user_id=user_id)
        if document_id is not None:
            q = q.filter_by(document_id=document_id)
        sessions = q.order_by(StudySession.updated_at.desc()).all()
        counts = dict(db.query(StudyMessage.session_id, func.count(StudyMessage.id))
                      .filter(StudyMessage.session_id.in_([s.id for s in sessions] or [0]),
                              StudyMessage.role == "assistant")
                      .group_by(StudyMessage.session_id).all())
        return [_session_dict(s, counts.get(s.id, 0)) for s in sessions]
    finally:
        db.close()


@app.get("/sessions/{session_id}")
def get_session(session_id: int, user_id: str = Depends(get_current_user)):
    db = SessionLocal()
    try:
        sess = _owned_session(db, session_id, user_id)
        doc = db.query(Document).filter_by(id=sess.document_id).first()
        turns = _session_turns(db, session_id)
        _attach_answers(db, session_id, turns)
        return {**_session_dict(sess, len(turns)), "document": _doc_dict(doc) if doc else None,
                "turns": turns}
    finally:
        db.close()


@app.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: int, user_id: str = Depends(get_current_user)):
    db = SessionLocal()
    try:
        sess = _owned_session(db, session_id, user_id)
        db.query(StudyMessage).filter_by(session_id=session_id).delete()
        db.query(QuizAttempt).filter_by(session_id=session_id).delete()
        db.delete(sess)
        db.commit()
    finally:
        db.close()


def _save_turn(session_id: int, user_id: str, document_id: int, message: str,
               turns: list[dict], result: dict) -> dict:
    """Store the turn in the conversation and log it in full for review. Returns the stored turn."""
    from datetime import UTC, datetime

    from engine.study import history_text
    turn = {"turn": len(turns) + 1, "message": message,
            **{k: v for k, v in result.items() if k not in _TURN_PRIVATE and k != "message"}}
    db = SessionLocal()
    try:
        sess = db.get(StudySession, session_id)
        db.add(StudyMessage(session_id=session_id, role="assistant",
                            content=json.dumps(turn, default=str)))
        if sess:
            sess.memory_json = json.dumps(result.get("memory") or {})
            sess.updated_at = datetime.now(UTC).replace(tzinfo=None)
            if not sess.title:
                sess.title = message[:80]
        db.commit()
    finally:
        db.close()

    record_interaction(
        user_id=user_id, kind="turn", request=message, document_id=document_id,
        record={**result, "session_id": session_id, "turn": turn["turn"],
                "history": history_text(turns)},
    )
    return turn


@app.post("/sessions/{session_id}/messages")
def send_message(session_id: int, body: MessageRequest, user_id: str = Depends(get_current_user)):
    """Run one turn of the conversation, streaming each step as server-sent events.

    Events: plan, step_start, step, block (one per output), reply, error, done (the stored turn).
    """
    from engine.study import run_turn

    db = SessionLocal()
    try:
        sess = _owned_session(db, session_id, user_id)
        doc = _owned_document(db, sess.document_id, user_id)
        if doc.status != "ready":
            raise HTTPException(status_code=409, detail=f"Document is not ready (status: {doc.status})")
        document_id = doc.id
        memory = json.loads(sess.memory_json) if sess.memory_json else None
        turns = _session_turns(db, session_id)
        db.add(StudyMessage(session_id=session_id, role="user", content=body.message))
        db.commit()
    finally:
        db.close()

    def event_generator():
        q: queue.Queue = queue.Queue()

        def emit(kind, payload):
            q.put({"type": kind, **payload})

        def run():
            try:
                result = run_turn(message=body.message, document_id=document_id, turns=turns,
                                  memory=memory, emit=emit)
                turn = _save_turn(session_id, user_id, document_id, body.message, turns, result)
                if result.get("error"):
                    q.put({"type": "error", "message": result["error"]})
                q.put({"type": "done", "turn": turn})
            except Exception as exc:
                import logging
                logging.getLogger(__name__).exception("turn failed in session %d", session_id)
                q.put({"type": "error", "message": f"Something went wrong: {exc}"})
            finally:
                q.put(None)

        threading.Thread(target=run, daemon=True).start()
        while (event := q.get()) is not None:
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/sessions/{session_id}/quiz-answers")
def answer_session_quiz(session_id: int, body: SessionQuizAnswers,
                        user_id: str = Depends(get_current_user)):
    """Grade answers to one quiz in the conversation and store them for progress tracking.

    Graded against the stored quiz, not anything the browser sends about correct answers.
    """
    db = SessionLocal()
    try:
        _owned_session(db, session_id, user_id)
        turn = next((t for t in _session_turns(db, session_id) if t.get("turn") == body.turn), None)
        block = next((b for b in (turn or {}).get("blocks") or []
                      if b.get("type") == "quiz" and b.get("step") == body.step), None)
        if block is None:
            raise HTTPException(status_code=404, detail="Quiz not found")
        items = block.get("items") or []
        results = []
        for number, chosen in sorted(body.answers.items()):
            if not 1 <= number <= len(items):
                raise HTTPException(status_code=422, detail=f"There's no question {number}")
            item = items[number - 1]
            correct = str(item.get("answer", "")).upper()
            chosen = chosen.strip().upper()[:1]
            db.add(QuizAttempt(
                user_id=user_id, session_id=session_id, turn=body.turn, step=body.step,
                question_idx=number - 1, question_text=str(item.get("question", ""))[:2000],
                chosen_answer=chosen or "-", correct_answer=correct[:1] or "-",
                is_correct=int(chosen == correct),
                chunk_id=(item.get("citation") or {}).get("chunk_id"),
            ))
            results.append({"question": number, "chosen": chosen, "correct": correct,
                            "is_correct": chosen == correct})
        db.commit()
    finally:
        db.close()
    right = sum(r["is_correct"] for r in results)
    return {"correct": right, "total": len(results), "results": results}


_EXPORT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@app.get("/sessions/{session_id}/export")
def export_session(
    session_id: int,
    format: str = Query(default="pdf", pattern="^(pdf|docx)$"),
    turn: Optional[int] = Query(default=None, ge=1),
    page_size: str = Query(default="A4", pattern="^(A4|letter)$"),
    user_id: str = Depends(get_current_user),
):
    """Download the conversation (or one turn of it) as a Word-style PDF or DOCX."""
    from engine.export import export_session as render
    from engine.export import safe_filename
    db = SessionLocal()
    try:
        sess = _owned_session(db, session_id, user_id)
        doc = db.query(Document).filter_by(id=sess.document_id).first()
        turns = _session_turns(db, session_id)
    finally:
        db.close()
    if turn is not None:
        turns = [t for t in turns if t.get("turn") == turn]
        if not turns:
            raise HTTPException(status_code=404, detail="Turn not found")
    doc_name = doc.filename if doc else "Document"
    title = (turns[0]["message"][:80] if turn is not None else sess.title) or "Study session"
    data = render(format, title, doc_name, turns, page_size=page_size)
    name = safe_filename(doc_name) + (f"-turn-{turn}" if turn else "-study") + f".{format}"
    return Response(content=data, media_type=_EXPORT_TYPES[format],
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})



def _doc_dict(d: Document) -> dict:
    return {
        "document_id": d.id, "filename": d.filename,
        "content_type": d.content_type, "size_bytes": d.size_bytes,
        "page_count": d.page_count, "char_count": d.char_count,
        "status": d.status, "error_message": d.error_message,
        "created_at": d.created_at,
    }


# ─── Search (Day 15) ──────────────────────────────────────────────────────────

@app.get("/search")
def search_library(
    q: str = Query(..., min_length=1),
    top_k: int = Query(default=10, ge=1, le=50),
    user_id: str = Depends(get_current_user),
):
    """Semantic search across all of the user's ready documents."""
    from engine.retrieval_node import retrieve_chunks

    # Collect document IDs owned by this user
    db = SessionLocal()
    try:
        doc_ids = [
            d.id for d in
            db.query(Document.id).filter_by(user_id=user_id, status="ready").all()
        ]
    finally:
        db.close()

    if not doc_ids:
        return {"query": q, "results": []}

    # For multi-doc search, run one query per doc and merge by score
    all_chunks: list[dict] = []
    for doc_id in doc_ids:
        all_chunks.extend(retrieve_chunks(query=q, document_id=doc_id, top_k=top_k))

    all_chunks.sort(key=lambda c: c["score"])
    return {"query": q, "results": all_chunks[:top_k]}


# ─── Workflow runs ────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """Deep health check: pings Postgres and Redis, returns 503 when degraded."""
    db_ok = False
    redis_ok = False
    db_error = None
    redis_error = None

    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        db_ok = True
    except Exception as e:
        db_error = str(e)

    try:
        import ssl as _ssl
        import urllib.parse as _urlparse

        import redis as _redis
        _redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        _p = _urlparse.urlparse(_redis_url)
        _r = _redis.Redis(
            host=_p.hostname,
            port=_p.port or 6379,
            password=_p.password,
            username=_p.username or "default",
            ssl=_redis_url.startswith("rediss://"),
            ssl_cert_reqs=_ssl.CERT_NONE,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        _r.ping()
        redis_ok = True
    except Exception as e:
        redis_error = str(e)

    overall = "ok" if (db_ok and redis_ok) else "degraded"
    payload = {
        "status": overall,
        "db": "ok" if db_ok else "error",
        "redis": "ok" if redis_ok else "error",
    }
    if db_error:
        payload["db_error"] = db_error
    if redis_error:
        payload["redis_error"] = redis_error
    return JSONResponse(payload, status_code=200 if overall == "ok" else 503)


@app.get("/runs")
def get_runs(user_id: str = Depends(get_current_user), db: Session = Depends(get_db)):
    runs = (db.query(WorkflowRun)
              .filter_by(user_id=user_id)
              .order_by(WorkflowRun.created_at.desc())
              .all())
    return [{"id": r.id, "goal": r.goal, "status": r.status, "steps": r.steps,
             "total_tokens": r.total_tokens, "total_time_ms": r.total_time_ms,
             "created_at": r.created_at} for r in runs]


MAX_DOC_CHARS = int(os.getenv("MAX_DOC_CHARS", "24000"))


_join_chunks = join_chunks


def _resolve_document_text(body: RunRequest, user_id: str) -> str:
    if body.document_id is None:
        return body.document
    db = SessionLocal()
    try:
        doc = db.query(Document).filter_by(id=body.document_id, user_id=user_id).first()
        if doc is None:
            raise HTTPException(status_code=404, detail="Document not found")
        chunks = (db.query(DocumentChunk)
                  .filter_by(document_id=doc.id)
                  .order_by(DocumentChunk.chunk_index)
                  .all())
    finally:
        db.close()
    if not chunks:
        raise HTTPException(status_code=409, detail=f"Document is not ready (status: {doc.status})")
    return _join_chunks(chunks)[:MAX_DOC_CHARS]


# ─── Supervisor review (admin) ────────────────────────────────────────────────
# Logs hold users' questions and document text, so these routes need ADMIN_TOKEN.
# With no ADMIN_TOKEN configured they don't exist.

def require_admin(x_admin_token: Optional[str] = Header(default=None)):
    expected = os.getenv("ADMIN_TOKEN")
    if not expected:
        raise HTTPException(status_code=404, detail="Not Found")
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=403, detail="Invalid admin token")


@app.get("/admin/logs", dependencies=[Depends(require_admin)])
def admin_pending_logs(limit: int = Query(default=20, ge=1, le=100)):
    """Logged interactions that haven't been reviewed yet, oldest first, with full records."""
    from engine.reviews import count_pending, pending_logs
    return {"pending_total": count_pending(), "logs": pending_logs(limit)}


@app.post("/admin/logs/{log_id}/review", dependencies=[Depends(require_admin)])
def admin_save_review(log_id: int, review: Review, reviewer: str = Query(default="claude-code")):
    from engine.reviews import save_review
    if not save_review(log_id, review, reviewer):
        raise HTTPException(status_code=404, detail="Log not found")
    return {"id": log_id, "review_status": "reviewed"}


@app.get("/admin/reviews/summary", dependencies=[Depends(require_admin)])
def admin_reviews_summary():
    """Review results per model, problems by role, and the latest lessons."""
    from engine.reviews import summarize
    return summarize()


def _log_run(user_id, body: RunRequest, plan, traces, output, document_text, run_id):
    from engine.config import LLM_MODEL, PLANNER_MODEL
    record_interaction(
        user_id=user_id, kind="run", request=body.request, document_id=body.document_id,
        record={
            "run_id": run_id,
            "goal": plan.goal,
            "brief": plan.brief.preferences(),
            "plan": [{"name": s.name, "instructions": s.description, "count": s.count,
                      "difficulty": s.difficulty, "input_key": s.input_key,
                      "output_key": s.output_key} for s in plan.steps],
            "steps": [{"name": t.name, "success": t.success, "error": t.error,
                       "time_ms": round(t.execution_time_ms),
                       "tokens": t.input_tokens + t.output_tokens} for t in traces],
            "output": output,
            "source": document_text,
            "models": {"planner": PLANNER_MODEL, "steps": LLM_MODEL},
        },
    )


@app.post("/run", response_model=RunResponse)
def run_workflow(body: RunRequest, user_id: str = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    document_text = _resolve_document_text(body, user_id)
    plan = parse_intent(body.request)
    engine = _build_engine(plan, body)
    initial_state = {"document": document_text, "query": body.request}
    final_state, traces = engine.run(plan, initial_state)

    step_results = [{"name": s.name, "input_key": s.input_key, "output_key": s.output_key}
                    for s in plan.steps]
    output = {k: v for k, v in final_state.items() if k not in ("document", "query")}
    total_tokens = sum(t.input_tokens + t.output_tokens for t in traces)
    total_time_ms = sum(t.execution_time_ms for t in traces)

    run = WorkflowRun(
        user_id=user_id, goal=plan.goal, request=body.request,
        status="success", steps=plan.estimated_steps,
        steps_json=json.dumps(step_results), summary=final_state.get("summary"),
        output_json=json.dumps(output, default=str),
        total_tokens=total_tokens, total_time_ms=total_time_ms,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    _log_run(user_id, body, plan, traces, output, document_text, run.id)

    return RunResponse(run_id=run.id, goal=plan.goal,
                       steps=[StepResult(**s) for s in step_results],
                       output=output, total_tokens=total_tokens, total_time_ms=total_time_ms)


@app.post("/run/stream")
def run_workflow_stream(body: RunRequest, user_id: str = Depends(get_current_user)):
    document_text = _resolve_document_text(body, user_id)

    def event_generator():
        try:
            plan = parse_intent(body.request)
            step_metas = [{"name": s.name, "input_key": s.input_key, "output_key": s.output_key}
                          for s in plan.steps]
            yield f"data: {json.dumps({'type': 'plan', 'goal': plan.goal, 'steps': step_metas, 'scope': plan.scope, 'too_broad': plan.too_broad, 'brief': plan.brief.preferences()})}\n\n"

            if plan.too_broad:
                yield f"data: {json.dumps({'type': 'error', 'message': plan.too_broad_reason or 'Request too broad'})}\n\n"
                return

            engine = _build_engine(plan, body)
            q: queue.Queue = queue.Queue()
            container: dict = {"state": None, "traces": [], "error": None}

            def _start(name): q.put(("step_start", name, None))
            def _end(name, trace): q.put(("step", name, trace))

            def _run():
                try:
                    state, traces = engine.run(
                        plan, {"document": document_text, "query": body.request},
                        on_step_start=_start, on_step_end=_end,
                    )
                    container["state"] = state
                    container["traces"] = traces
                except Exception as e:
                    container["error"] = str(e)
                finally:
                    q.put(("done", None, None))

            threading.Thread(target=_run, daemon=True).start()

            while True:
                ev, name, trace = q.get()
                if ev == "done":
                    break
                if ev == "step_start":
                    yield f"data: {json.dumps({'type': 'step_start', 'name': name})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'step', 'name': name, 'success': trace.success, 'time_ms': round(trace.execution_time_ms or 0), 'tokens': (trace.input_tokens or 0)+(trace.output_tokens or 0), 'error': trace.error})}\n\n"

            if container["error"]:
                yield f"data: {json.dumps({'type': 'error', 'message': container['error']})}\n\n"
                return

            final_state = container["state"]
            traces = container["traces"]
            output = {k: v for k, v in final_state.items() if k not in ("document", "query")}
            total_tokens = sum(t.input_tokens + t.output_tokens for t in traces)
            total_time_ms = sum(t.execution_time_ms for t in traces)

            db = SessionLocal()
            try:
                run = WorkflowRun(
                    user_id=user_id, goal=plan.goal, request=body.request,
                    status="success", steps=plan.estimated_steps,
                    steps_json=json.dumps(step_metas),
                    summary=final_state.get("summary"),
                    output_json=json.dumps(output, default=str),
                    total_tokens=total_tokens, total_time_ms=total_time_ms,
                )
                db.add(run)
                db.commit()
                db.refresh(run)
                run_id = run.id
            finally:
                db.close()
            _log_run(user_id, body, plan, traces, output, document_text, run_id)

            yield f"data: {json.dumps({'type': 'done', 'run_id': run_id, 'goal': plan.goal, 'steps': step_metas, 'output': output, 'total_tokens': total_tokens, 'total_time_ms': total_time_ms}, default=str)}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/runs/{run_id}")
def get_run(run_id: int, user_id: str = Depends(get_current_user),
            db: Session = Depends(get_db)):
    run = db.query(WorkflowRun).filter_by(id=run_id, user_id=user_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"run_id": run.id, "goal": run.goal, "status": run.status,
            "steps": json.loads(run.steps_json) if run.steps_json else [],
            "total_tokens": run.total_tokens, "total_time_ms": run.total_time_ms,
            "output": json.loads(run.output_json) if run.output_json else {},
            "created_at": run.created_at}


# ─── Quiz attempts (Day 13) ───────────────────────────────────────────────────

@app.post("/quiz/attempt")
def submit_quiz_attempt(
    body: QuizAttemptRequest,
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Record the user's answers for a quiz and return the score."""
    attempts = []
    correct_count = 0
    for item in body.answers:
        is_correct = item.chosen_answer.upper() == item.correct_answer.upper()
        if is_correct:
            correct_count += 1
        attempt = QuizAttempt(
            user_id=user_id, run_id=body.run_id,
            question_idx=item.question_idx, question_text=item.question_text,
            chosen_answer=item.chosen_answer.upper(),
            correct_answer=item.correct_answer.upper(),
            is_correct=int(is_correct),
        )
        db.add(attempt)
        attempts.append(attempt)
    db.commit()
    total = len(body.answers)
    return {
        "run_id": body.run_id,
        "score": correct_count,
        "total": total,
        "pct": round(correct_count / total * 100) if total else 0,
    }


@app.get("/quiz/history")
def quiz_history(user_id: str = Depends(get_current_user), db: Session = Depends(get_db)):
    attempts = (db.query(QuizAttempt)
                  .filter_by(user_id=user_id)
                  .order_by(QuizAttempt.created_at.desc())
                  .limit(200).all())
    return [
        {"run_id": a.run_id, "question_idx": a.question_idx,
         "chosen_answer": a.chosen_answer, "correct_answer": a.correct_answer,
         "is_correct": bool(a.is_correct), "created_at": a.created_at}
        for a in attempts
    ]


# ─── Exports (Days 16-17) ─────────────────────────────────────────────────────

@app.get("/runs/{run_id}/export/pdf")
def export_pdf(run_id: int, user_id: str = Depends(get_current_user),
               db: Session = Depends(get_db)):
    run = db.query(WorkflowRun).filter_by(id=run_id, user_id=user_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    from engine.export import generate_pdf
    pdf_bytes = generate_pdf(run)
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="study-kit-{run_id}.pdf"'})


@app.get("/runs/{run_id}/export/docx")
def export_docx(run_id: int, user_id: str = Depends(get_current_user),
                db: Session = Depends(get_db)):
    run = db.query(WorkflowRun).filter_by(id=run_id, user_id=user_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    from engine.export import generate_docx
    return Response(content=generate_docx(run), media_type=_EXPORT_TYPES["docx"],
                    headers={"Content-Disposition": f'attachment; filename="study-kit-{run_id}.docx"'})


@app.get("/runs/{run_id}/export/anki")
def export_anki(run_id: int, user_id: str = Depends(get_current_user),
                db: Session = Depends(get_db)):
    run = db.query(WorkflowRun).filter_by(id=run_id, user_id=user_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    from engine.export import generate_anki
    apkg_bytes = generate_anki(run)
    return Response(content=apkg_bytes, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="study-kit-{run_id}.apkg"'})


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _build_engine(plan, body: RunRequest) -> ExecutionEngine:
    """Build an ExecutionEngine with nodes for *plan*, injecting difficulty."""
    eng = ExecutionEngine()
    for step in plan.steps:
        node = build_node_for_step(
            name=step.name, description=step.description,
            input_key=step.input_key, output_key=step.output_key,
        )
        if hasattr(node, "_difficulty"):
            node._difficulty = body.difficulty or "medium"
        if isinstance(node, RetrieveChunksNode):
            node.document_id = body.document_id
            node.require_document = True
        if hasattr(node, "set_context"):
            brief = plan.brief.for_step(step.count, step.difficulty)
            node.set_context(StepContext(request=body.request, goal=plan.goal,
                                         instructions=step.description, brief=brief))
        eng.register_node(step.name, node)
    return eng


# ─── Frontend static files ────────────────────────────────────────────────────
# Serve React build when frontend/dist exists (production Docker image).
# Must be mounted after all API routes so API paths take priority.
_frontend_dist = os.path.join(os.path.dirname(__file__), "frontend", "dist")
if os.path.exists(_frontend_dist):
    app.mount("/assets", StaticFiles(directory=os.path.join(_frontend_dist, "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_frontend(full_path: str):
        return FileResponse(os.path.join(_frontend_dist, "index.html"))
