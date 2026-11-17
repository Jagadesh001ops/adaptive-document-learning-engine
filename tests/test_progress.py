"""Progress through a document: the section map, coverage, and the suggested next task."""
from types import SimpleNamespace
from unittest.mock import patch

from engine import progress

SECTIONS = [
    {"section": 1, "title": "Print arrives", "chunk_ids": [1, 2], "pages": [1, 3]},
    {"section": 2, "title": "The Church reacts", "chunk_ids": [3, 4], "pages": [4, 6]},
    {"section": 3, "title": "Reading mania", "chunk_ids": [5, 6], "pages": [7, 9]},
]


def _session(*turns, sid=1):
    return {"session_id": sid, "updated_at": None, "turns": list(turns)}


def _quiz(turn, chunks, step=0, **extra):
    items = [{"question": f"Q{i}", "answer": "A", "citation": {"chunk_id": c}} for i, c in enumerate(chunks)]
    return {"turn": turn, "message": "quiz", "blocks": [{"type": "quiz", "step": step, "items": items, **extra}]}


def _summary(turn, chunks):
    return {"turn": turn, "message": "summarize",
            "blocks": [{"type": "summary", "step": 0, "text": "...", "chunk_ids": chunks}]}


def _answers(turn, results, sid=1, step=0, start_id=1):
    """results: list of (chunk_id, is_correct) in question order."""
    return [{"id": start_id + i, "session_id": sid, "turn": turn, "step": step, "question_idx": i,
             "chunk_id": c, "is_correct": ok} for i, (c, ok) in enumerate(results)]


# ── outline ───────────────────────────────────────────────────────────────────

def _rows(n, size=100):
    return [SimpleNamespace(id=10 + i, chunk_index=i, char_start=i * size, char_end=(i + 1) * size,
                            text=f"Chunk {i} text.") for i in range(n)]


def test_outline_splits_chunks_into_titled_sections_with_page_ranges():
    doc = SimpleNamespace(id=1, page_count=10, char_count=2000)
    with patch("engine.progress._titles", side_effect=lambda ex: [f"T{i}" for i in range(len(ex))]):
        out = progress.build_outline(doc, _rows(20))
    assert out["titled"] and len(out["sections"]) == progress.MAX_SECTIONS
    flat = [c for s in out["sections"] for c in s["chunk_ids"]]
    assert flat == [r.id for r in _rows(20)], "sections are consecutive and cover every chunk once"
    assert out["sections"][0]["pages"][0] == 1 and out["sections"][-1]["pages"][1] == 10
    assert [s["title"] for s in out["sections"]][:2] == ["T0", "T1"]


def test_outline_falls_back_to_part_numbers_when_titling_fails():
    doc = SimpleNamespace(id=1, page_count=None, char_count=None)
    with patch("engine.progress._titles", side_effect=ValueError("busy")):
        out = progress.build_outline(doc, _rows(3))
    assert not out["titled"]
    assert [s["title"] for s in out["sections"]] == ["Part 1", "Part 2", "Part 3"]
    assert out["sections"][0]["pages"] is None
    assert progress.build_outline(doc, []) == {"sections": [], "titled": False}


def test_titles_must_match_the_number_of_sections():
    with patch("engine.qa._chat", return_value=({"titles": ["only one"]}, 5)):
        try:
            progress._titles(["a", "b"])
        except ValueError:
            pass
        else:
            raise AssertionError("a short title list should be rejected")
    with patch("engine.qa._chat", return_value=({"titles": ["One", ""]}, 5)):
        assert progress._titles(["a", "b"]) == ["One", "Part 2"]


def test_outline_text_lists_sections_for_the_planner():
    text = progress.outline_text(SECTIONS)
    assert "1. Print arrives (about pages 1-3)" in text and "3. Reading mania" in text


# ── next task ─────────────────────────────────────────────────────────────────

def test_a_new_document_starts_with_an_overview():
    out = progress.summarize_progress(SECTIONS, [], [])
    assert out["next"]["kind"] == "overview" and out["next"]["message"]
    assert all(s["status"] == "new" for s in out["sections"])


def test_an_unanswered_quiz_comes_first():
    out = progress.summarize_progress(SECTIONS, [_session(_summary(1, [1]), _quiz(2, [1, 2]))], [])
    nxt = out["next"]
    assert nxt["kind"] == "finish_quiz" and nxt["session_id"] == 1 and nxt["turn"] == 2
    assert nxt["message"] is None
    assert out["sections"][0]["status"] == "quizzed"


def test_a_quiz_replaced_by_a_revision_is_not_left_open():
    revised = _quiz(2, [1, 2], revised_from={"turn": 1, "item": 2})
    out = progress.summarize_progress(SECTIONS, [_session(_quiz(1, [1, 2]), revised)],
                                      _answers(2, [(1, True), (2, True)]))
    assert out["next"]["kind"] != "finish_quiz"
    assert out["done"]["quizzes"] == 1, "a revision isn't counted as a new quiz"


def test_missed_questions_send_you_back_to_that_section():
    sessions = [_session(_summary(1, [1, 2, 3]), _quiz(2, [1, 3, 4]))]
    out = progress.summarize_progress(SECTIONS, sessions, _answers(2, [(1, True), (3, False), (4, False)]))
    assert out["next"]["kind"] == "revisit" and out["next"]["section"] == 2
    assert "section 2 (The Church reacts)" in out["next"]["message"]
    s1, s2, _ = out["sections"]
    assert (s1["status"], s1["right"], s1["wrong"]) == ("tested", 1, 0)
    assert (s2["status"], s2["wrong"]) == ("tested", 2)
    assert out["done"]["answered"] == 3 and out["done"]["correct"] == 1


def test_a_newer_quiz_on_the_section_replaces_the_old_result():
    sessions = [_session(_quiz(1, [3]), _quiz(2, [4]))]
    attempts = _answers(1, [(3, False)]) + _answers(2, [(4, True)], start_id=5)
    out = progress.summarize_progress(SECTIONS, sessions, attempts)
    assert out["sections"][1]["wrong"] == 0 and out["next"]["kind"] != "revisit"


def test_suggestions_walk_through_the_document_in_order():
    read_one = [_session(_summary(1, [1, 2]))]
    out = progress.summarize_progress(SECTIONS, read_one, [])
    assert out["next"]["kind"] == "test" and out["next"]["section"] == 1
    assert out["sections"][0]["coverage"] == 1.0

    tested_one = [_session(_summary(1, [1, 2]), _quiz(2, [1, 2]))]
    out = progress.summarize_progress(SECTIONS, tested_one, _answers(2, [(1, True), (2, True)]))
    assert out["next"]["kind"] == "study" and out["next"]["section"] == 2
    assert "Summarize section 2 (The Church reacts)" in out["next"]["message"]


def test_after_every_section_comes_a_whole_document_test_then_review():
    sessions = [_session(_quiz(1, [1, 3, 5]))]
    attempts = _answers(1, [(1, True), (3, True), (5, True)])
    assert progress.summarize_progress(SECTIONS, sessions, attempts)["next"]["kind"] == "final"

    whole = [_session(_quiz(1, [1, 3, 5], scope="whole"))]
    assert progress.summarize_progress(SECTIONS, whole, attempts)["next"]["kind"] == "review"


def test_done_counts_each_kind_of_output():
    turn = {"turn": 1, "message": "m", "blocks": [
        {"type": "answer", "citations": [{"chunk_id": 5}]}, {"type": "concepts", "chunk_ids": [6]},
        {"type": "note", "text": "nothing"}]}
    out = progress.summarize_progress(SECTIONS, [_session(turn)], [])
    assert out["done"]["questions_asked"] == 1 and out["done"]["concepts"] == 1
    assert out["sections"][2]["status"] == "read" and out["sections"][2]["coverage"] == 1.0
