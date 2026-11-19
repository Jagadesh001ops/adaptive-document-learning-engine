"""Where a student is in a document: its sections, what they've done, and what to do next.

A document is split into up to MAX_SECTIONS sections of consecutive chunks. PLANNER_MODEL gives
each a short title once, and the outline is stored on the document. The study planner sees the
outline, so "summarize section 3" or "quiz me on the reading mania part" reads that section.

Progress comes from the study sessions: each output records the passages it was built from or
quotes, and each quiz answer records the passage its question came from. The next task is picked
by rules in code, walking through the document in order: finish an open quiz, go back over a
section answered badly, then study (and then test) the first section not yet tested, and finally
test the whole document.
"""
from __future__ import annotations

import json
import logging
from typing import List, Optional

from .config import PLANNER_MODEL

logger = logging.getLogger(__name__)

MAX_SECTIONS = 8
TITLE_CHARS = 700        # start of each section shown to the model that titles it

_TITLE_SYSTEM = """You name the sections of a document for a student's study map. For each
numbered excerpt (the start of one section), write a short title of 2 to 6 words saying what
that section is about. Use the document's own terms. Return ONLY JSON:
{"titles": ["...", "..."]} with exactly one title per excerpt, in order."""


# ── outline ───────────────────────────────────────────────────────────────────

def _pages(doc, first, last) -> Optional[List[int]]:
    """Approximate page range from character offsets (chunks don't record pages)."""
    if not doc.page_count or not doc.char_count:
        return None
    per = doc.char_count / doc.page_count
    start = min(doc.page_count, int(first.char_start / per) + 1)
    end = min(doc.page_count, max(start, int(max(last.char_end - 1, 0) / per) + 1))
    return [start, end]


def _titles(excerpts: List[str]) -> List[str]:
    from .qa import _chat
    user = "\n\n".join(f"Section {i}:\n{text}" for i, text in enumerate(excerpts, 1))
    data, _ = _chat(PLANNER_MODEL, _TITLE_SYSTEM, user, json_mode=True)
    titles = data.get("titles") if isinstance(data, dict) else None
    if not isinstance(titles, list) or len(titles) != len(excerpts):
        raise ValueError("the model didn't return one title per section")
    return [str(t).strip()[:80] or f"Part {i}" for i, t in enumerate(titles, 1)]


def build_outline(doc, rows) -> dict:
    """Split ordered chunk rows into sections and title them. Titles fall back to "Part N"."""
    if not rows:
        return {"sections": [], "titled": False}
    k = min(MAX_SECTIONS, len(rows))
    bounds = [round(i * len(rows) / k) for i in range(k + 1)]
    parts = [rows[bounds[i]:bounds[i + 1]] for i in range(k)]
    sections = [{"section": i + 1, "title": f"Part {i + 1}", "chunk_ids": [r.id for r in part],
                 "pages": _pages(doc, part[0], part[-1])}
                for i, part in enumerate(parts)]
    titled = False
    try:
        for s, title in zip(sections, _titles([" ".join(r.text for r in part)[:TITLE_CHARS]
                                               for part in parts]), strict=True):
            s["title"] = title
        titled = True
    except Exception as exc:   # titles are a nicety; the map still works without them
        logger.warning("could not title the sections of document %s: %s", doc.id, exc)
    return {"sections": sections, "titled": titled}


def get_outline(document_id: int, *, build: bool = True) -> Optional[List[dict]]:
    """The document's sections, building and storing them if needed.

    With build=False, returns None when nothing usable is stored yet. An outline whose titles
    failed is rebuilt on the next call that allows building.
    """
    from .database import Document, DocumentChunk, SessionLocal
    db = SessionLocal()
    try:
        doc = db.get(Document, document_id)
        if not doc or doc.status != "ready":
            return []
        stored = json.loads(doc.outline_json) if doc.outline_json else None
        if stored and (stored.get("titled") or not build):
            return stored["sections"]
        if not build:
            return None
        rows = (db.query(DocumentChunk).filter_by(document_id=document_id)
                .order_by(DocumentChunk.chunk_index).all())
        data = build_outline(doc, rows)
        if data["sections"]:
            doc.outline_json = json.dumps(data)
            db.commit()
        return data["sections"]
    finally:
        db.close()


def outline_text(sections: List[dict]) -> str:
    lines = []
    for s in sections:
        pages = f" (about pages {s['pages'][0]}-{s['pages'][1]})" if s.get("pages") else ""
        lines.append(f"{s['section']}. {s['title']}{pages}")
    return "\n".join(lines)


# ── progress ──────────────────────────────────────────────────────────────────

def block_chunks(block: dict) -> List[int]:
    """Passages an output was built from or quotes."""
    ids = list(block.get("chunk_ids") or [])
    ids += [c.get("chunk_id") for c in block.get("citations") or []]
    ids += [(q.get("citation") or {}).get("chunk_id") for q in block.get("items") or []]
    return [i for i in ids if isinstance(i, int)]


def _superseded(turns: List[dict]) -> set:
    """Turns whose quiz a later revise replaced."""
    out = set()
    for t in turns:
        for b in t.get("blocks") or []:
            src = b.get("revised_from") or {}
            if b.get("type") == "quiz" and src.get("turn"):
                out.add(src["turn"])
    return out


def summarize_progress(sections: List[dict], sessions: List[dict], attempts: List[dict]) -> dict:
    """Pure function: sections from the outline; sessions as {session_id, updated_at, turns};
    attempts as {session_id, turn, step, question_idx, chunk_id, is_correct, id}."""
    where = {cid: s["section"] for s in sections for cid in s["chunk_ids"]}
    state = {s["section"]: {"read": set(), "quizzed": set(), "right": 0, "wrong": 0, "last": -1}
             for s in sections}
    done = {"sessions": len(sessions), "questions_asked": 0, "summaries": 0, "concepts": 0,
            "quizzes": 0, "answered": 0, "correct": 0}

    # latest answer per question
    latest = {}
    for a in sorted(attempts, key=lambda a: a["id"]):
        latest[(a["session_id"], a["turn"], a["step"], a["question_idx"])] = a
    answered_blocks = {(a["session_id"], a["turn"], a["step"]) for a in latest.values()}
    done["answered"] = len(latest)
    done["correct"] = sum(1 for a in latest.values() if a["is_correct"])

    # per section, judge only the most recent quiz that tested it
    by_block: dict = {}
    for a in latest.values():
        by_block.setdefault((a["session_id"], a["turn"], a["step"]), []).append(a)
    for answers in by_block.values():
        newest = max(a["id"] for a in answers)
        for sec in {where.get(a["chunk_id"]) for a in answers} - {None}:
            st = state[sec]
            if newest > st["last"]:
                mine = [a for a in answers if where.get(a["chunk_id"]) == sec]
                st.update(last=newest, right=sum(1 for a in mine if a["is_correct"]),
                          wrong=sum(1 for a in mine if not a["is_correct"]))

    open_quiz = None
    whole_quiz_answered = False
    for sess in sessions:
        replaced = _superseded(sess["turns"])
        for t in sess["turns"]:
            for b in t.get("blocks") or []:
                kind = b.get("type")
                chunks = block_chunks(b)
                if kind == "answer":
                    done["questions_asked"] += 1
                elif kind == "summary":
                    done["summaries"] += 1
                elif kind == "concepts":
                    done["concepts"] += 1
                if kind == "quiz":
                    if not b.get("revised_from"):
                        done["quizzes"] += 1
                    key = (sess["session_id"], t.get("turn"), b.get("step"))
                    if key in answered_blocks:
                        if b.get("scope") == "whole":
                            whole_quiz_answered = True
                    elif t.get("turn") not in replaced and b.get("items"):
                        open_quiz = {"session_id": sess["session_id"], "turn": t.get("turn"),
                                     "questions": len(b["items"])}
                    for c in chunks:
                        if c in where:
                            state[where[c]]["quizzed"].add(c)
                elif kind in ("answer", "summary", "concepts") and b.get("scope") != "whole":
                    # An overview of the whole document skims every section; it doesn't count as
                    # having studied any one of them.
                    for c in chunks:
                        if c in where:
                            state[where[c]]["read"].add(c)

    out_sections = []
    for s in sections:
        st = state[s["section"]]
        touched = st["read"] | st["quizzed"]
        if st["right"] or st["wrong"]:
            status = "tested"
        elif st["quizzed"]:
            status = "quizzed"
        elif st["read"]:
            status = "read"
        else:
            status = "new"
        out_sections.append({
            "section": s["section"], "title": s["title"], "pages": s.get("pages"), "status": status,
            "coverage": round(len(touched) / len(s["chunk_ids"]), 2) if s["chunk_ids"] else 0,
            "right": st["right"], "wrong": st["wrong"],
        })

    return {"sections": out_sections, "done": done,
            "next": next_task(out_sections, done, open_quiz, whole_quiz_answered)}


def _name(s: dict) -> str:
    return f"section {s['section']} ({s['title']})"


def next_task(sections: List[dict], done: dict, open_quiz: Optional[dict],
              whole_quiz_answered: bool) -> dict:
    """One suggestion. `message` is what gets sent to the study session when it's chosen."""
    started = any(done[k] for k in ("questions_asked", "summaries", "concepts", "quizzes"))
    if not started:
        return {"kind": "overview", "label": "Get an overview",
                "detail": "Start with the big picture before going section by section.",
                "message": "Give me an overview of the whole document in 5 bullet points"}
    if open_quiz:
        n = open_quiz["questions"]
        return {"kind": "finish_quiz", "label": "Finish your quiz",
                "detail": f"{n} question{'s' if n != 1 else ''} from turn {open_quiz['turn']} "
                          "not answered yet.",
                "message": None, "session_id": open_quiz["session_id"], "turn": open_quiz["turn"]}
    weak = [s for s in sections if s["wrong"]]
    if weak:
        s = max(weak, key=lambda s: (s["wrong"], -s["section"]))
        return {"kind": "revisit", "label": f"Go back over {s['title']}",
                "detail": f"You missed {s['wrong']} question{'s' if s['wrong'] != 1 else ''} "
                          f"on section {s['section']}.",
                "message": f"Explain the main points of {_name(s)} again, then give me 3 questions on it",
                "section": s["section"]}
    for s in sections:
        if s["status"] == "new":
            return {"kind": "study", "label": f"Study {s['title']}",
                    "detail": f"Section {s['section']} of {len(sections)} hasn't been covered yet.",
                    "message": f"Summarize {_name(s)} in 5 bullet points, then give me 3 questions on it",
                    "section": s["section"]}
        if s["status"] == "read":
            return {"kind": "test", "label": f"Test yourself on {s['title']}",
                    "detail": f"You've read section {s['section']} but haven't been quizzed on it.",
                    "message": f"Give me 3 questions on {_name(s)}",
                    "section": s["section"]}
    if not whole_quiz_answered:
        return {"kind": "final", "label": "Test yourself on the whole document",
                "detail": "Every section has been studied and tested.",
                "message": "Quiz me on the whole document: 8 hard questions"}
    return {"kind": "review", "label": "Review the hardest parts",
            "detail": "You've covered everything. A mixed review keeps it fresh.",
            "message": "Give me 5 hard questions mixing different sections of the document"}


def document_progress(document_id: int, user_id: str) -> dict:
    from .database import QuizAttempt, SessionLocal, StudyMessage, StudySession
    sections = get_outline(document_id) or []
    db = SessionLocal()
    try:
        sess_rows = (db.query(StudySession).filter_by(document_id=document_id, user_id=user_id)
                     .order_by(StudySession.updated_at.desc()).all())
        ids = [s.id for s in sess_rows]
        msgs = (db.query(StudyMessage)
                .filter(StudyMessage.session_id.in_(ids or [0]), StudyMessage.role == "assistant")
                .order_by(StudyMessage.id).all())
        rows = (db.query(QuizAttempt).filter(QuizAttempt.session_id.in_(ids or [0]))
                .order_by(QuizAttempt.id).all())
    finally:
        db.close()
    turns: dict = {}
    for m in msgs:
        turns.setdefault(m.session_id, []).append(json.loads(m.content))
    sessions = [{"session_id": s.id, "updated_at": s.updated_at, "turns": turns.get(s.id, [])}
                for s in sess_rows]
    attempts = [{"id": a.id, "session_id": a.session_id, "turn": a.turn, "step": a.step,
                 "question_idx": a.question_idx, "chunk_id": a.chunk_id,
                 "is_correct": bool(a.is_correct)} for a in rows]
    out = summarize_progress(sections, sessions, attempts)
    out["document_id"] = document_id
    out["last_studied"] = sess_rows[0].updated_at if sess_rows and any(turns.values()) else None
    out["latest_session_id"] = sess_rows[0].id if sess_rows else None
    return out
