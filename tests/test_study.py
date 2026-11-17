"""Study sessions: turn planning, combined tools, per-step settings, revisions and memory."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from engine import qa, study
from engine.config import ANSWER_MODEL, CHECK_MODEL, LLM_MODEL, PLANNER_MODEL
from engine.llm_nodes import attach_citations

P1 = {"chunk_id": 1, "document_id": 7, "char_start": 0, "char_end": 56,
      "text": "Gutenberg built the printing press around 1440 in Mainz."}
P2 = {"chunk_id": 2, "document_id": 7, "char_start": 50, "char_end": 140,
      "text": "The Church feared printed books spread heresy, so it issued the Index of Prohibited Books."}


class FakeClient:
    """Scripted model replies, in call order. Dicts are sent as JSON; exceptions are raised."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, model, messages, **kwargs):
        self.calls.append({"model": model, "system": messages[0]["content"], "user": messages[1]["content"]})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                               usage=SimpleNamespace(prompt_tokens=50, completion_tokens=10))


def _step(tool, **kw):
    return {"tool": tool, "instructions": kw.pop("instructions", f"do {tool}"), **kw}


def _plan(*steps, query="printing press", reply=None):
    return {"goal": "study", "search_query": query, "steps": list(steps), "reply": reply}


def _quiz_item(q="Who built the press?", excerpt="Gutenberg built the printing press", chunk=1):
    return {"question": q, "options": {"A": "Gutenberg", "B": "Luther", "C": "Polo", "D": "Bi Sheng"},
            "answer": "A", "explanation": "Passage 1.", "citation": {"chunk_id": chunk, "excerpt": excerpt}}


ANSWER = {"answer": "Gutenberg, around 1440.", "justification": "Passage 1.",
          "citations": [{"chunk_id": 1, "quote": "Gutenberg built the printing press"}],
          "status": "answered", "general_knowledge": None, "follow_ups": ["Where?"]}
SUPPORTED = {"supported": True, "problems": [], "missing": None, "search_query": None}


def _run(client, message, turns=(), memory=None, remembered=(), whole_text="Whole document text.",
         outline=()):
    events = []
    searches = []

    def fake_retrieve(query, document_id, top_k):
        searches.append(query)
        return [dict(P1), dict(P2)]

    with (
        patch("engine.llm_nodes.get_client", return_value=client),
        patch("engine.retrieval_node.retrieve_chunks", side_effect=fake_retrieve),
        patch("engine.study.load_chunks", side_effect=lambda doc, ids: [p for p in (P1, P2) if p["chunk_id"] in ids]
              if ids and not remembered else list(remembered)),
        patch("engine.study.spread_chunks", return_value=[dict(P1), dict(P2)]),
        patch("engine.study.document_text", return_value=(whole_text, [1, 2])),
        patch("engine.progress.get_outline", return_value=list(outline)),
    ):
        result = study.run_turn(message=message, document_id=7, turns=list(turns), memory=memory,
                                emit=lambda kind, payload: events.append((kind, payload)))
    return result, events, searches


# ── combining tools in one message ────────────────────────────────────────────

def test_one_message_can_answer_and_then_quiz_on_the_answer():
    client = FakeClient(
        _plan(_step("answer", question="Who built the press?"),
              _step("quiz", count=2, difficulty="hard", uses_previous_step=True)),
        ANSWER, SUPPORTED,
        {"items": [_quiz_item(), _quiz_item("Where?"), _quiz_item("When?")]},
    )
    result, events, _ = _run(client, "Who built the press? Then give me 2 hard questions on it")

    assert result["error"] is None
    assert [b["type"] for b in result["blocks"]] == ["answer", "quiz"]
    assert result["blocks"][0]["verification"] == "checked"
    quiz = result["blocks"][1]
    assert len(quiz["items"]) == 2, "the requested count is enforced in code"
    assert quiz["difficulty"] == "hard"
    assert [c["model"] for c in client.calls] == [PLANNER_MODEL, ANSWER_MODEL, CHECK_MODEL, LLM_MODEL]
    # The quiz builds on the answer and is grounded in document passages.
    assert "Gutenberg, around 1440." in client.calls[3]["system"]
    assert "[chunk_id=1]" in client.calls[3]["user"]
    assert [p["block"]["type"] for k, p in events if k == "block"] == ["answer", "quiz"]
    assert {p["chunk_id"] for p in result["passages"]} == {1, 2}


def test_each_step_gets_its_own_count_so_numbers_do_not_leak():
    client = FakeClient(
        _plan(_step("summarize", count=4, format="bullet points", scope="whole"),
              _step("quiz", count=3, difficulty="hard", scope="whole")),
        "- a\n- b\n- c\n- d",
        {"items": [_quiz_item(f"Q{i}") for i in range(5)]},
    )
    result, _, _ = _run(client, "Summarize as 4 bullet points and give me 3 hard questions")

    summary_prompt, quiz_prompt = client.calls[1]["system"], client.calls[2]["system"]
    assert "count: 4" in summary_prompt and "bullet points" in summary_prompt
    assert "exactly 3" in quiz_prompt and "hard" in quiz_prompt
    assert "count: 4" not in quiz_prompt and "exactly 4" not in quiz_prompt
    assert len(result["blocks"][1]["items"]) == 3
    assert result["blocks"][0]["text"].startswith("- a")
    assert result["sources"]["document"] == "Whole document text."


def test_a_greeting_gets_a_reply_and_runs_no_tools():
    client = FakeClient(_plan(reply="Hi! Ask me anything about the document."))
    result, events, searches = _run(client, "hello")
    assert result["blocks"] == [] and result["reply"].startswith("Hi!")
    assert ("reply", {"text": "Hi! Ask me anything about the document."}) in events
    assert searches == [] and len(client.calls) == 1


def test_a_rate_limit_is_reported_as_an_error_not_a_crash():
    class RateLimitError(Exception):
        pass

    result, _, _ = _run(FakeClient(RateLimitError("429")), "summarize")
    assert result["error"].startswith("The model is busy") and result["blocks"] == []


def test_a_failing_step_keeps_the_blocks_already_produced():
    client = FakeClient(_plan(_step("summarize"), _step("quiz")), "Short summary.", "not json at all")
    result, _, _ = _run(client, "summarize then quiz")
    assert [b["type"] for b in result["blocks"]] == ["summary"]
    assert result["error"].startswith("Something went wrong")
    assert result["steps"][-1]["name"] == "quiz" and result["steps"][-1]["success"] is False


# ── revisions change exactly what was asked ───────────────────────────────────

def _earlier_quiz_turn():
    items = [_quiz_item("Q1"), _quiz_item("Q2"), _quiz_item("Q3")]
    return {"turn": 1, "message": "3 questions", "blocks": [{"type": "quiz", "items": items, "step": 0}]}


def test_revising_one_question_leaves_the_others_untouched():
    earlier = _earlier_quiz_turn()
    harder = _quiz_item("Why did the Church issue the Index?", "feared printed books spread heresy", chunk=2)
    client = FakeClient(_plan(_step("revise", target_turn=1, target_item=2,
                                    instructions="Make question 2 harder")),
                        {"item": harder})
    result, _, _ = _run(client, "make question 2 harder", turns=[earlier])

    [block] = result["blocks"]
    assert block["type"] == "quiz" and block["revised_from"] == {"turn": 1, "item": 2}
    assert [q["question"] for q in block["items"]] == ["Q1", "Why did the Church issue the Index?", "Q3"]
    assert block["items"][0] == earlier["blocks"][0]["items"][0]
    assert block["items"][1]["verified"] is True
    assert result["targets"][0]["turn"] == 1 and result["targets"][0]["item"] == 2
    # Only the named question is sent to the model.
    assert '"Q2"' in client.calls[1]["user"] and '"Q1"' not in client.calls[1]["user"]


def test_revising_a_question_that_does_not_exist_says_so():
    client = FakeClient(_plan(_step("revise", target_turn=1, target_item=9)))
    result, _, _ = _run(client, "change question 9", turns=[_earlier_quiz_turn()])
    assert result["blocks"][0]["type"] == "note" and "9" in result["blocks"][0]["text"]
    assert len(client.calls) == 1


def test_revising_with_nothing_earlier_says_so():
    client = FakeClient(_plan(_step("revise")))
    result, _, _ = _run(client, "make it shorter")
    assert result["blocks"][0]["type"] == "note"


def test_revising_a_summary_defaults_to_the_latest_one():
    turns = [{"turn": 1, "message": "summarize", "blocks": [{"type": "summary", "text": "Old long summary."}]},
             {"turn": 2, "message": "hello", "blocks": [], "reply": "Hi"}]
    client = FakeClient(_plan(_step("revise", instructions="shorter")), {"text": "Short."})
    result, _, _ = _run(client, "make it shorter", turns=turns)
    assert result["blocks"][0] == {"type": "summary", "text": "Short.", "scope": None, "section": None,
                                   "chunk_ids": [1, 2], "revised_from": {"turn": 1, "item": None},
                                   "step": 0}


# ── memory ────────────────────────────────────────────────────────────────────

def test_follow_ups_see_the_conversation_and_remembered_passages():
    turns = [{"turn": 1, "message": "Who built the press?", "blocks": [{"type": "answer", **ANSWER}]}]
    client = FakeClient(_plan(_step("answer", question="Why did the Church fear it?"),
                              query="Church fear of the printing press"),
                        ANSWER, SUPPORTED)
    remembered = [{"chunk_id": 9, "document_id": 7, "text": "Remembered passage.", "char_start": 0,
                   "char_end": 5}]
    result, _, searches = _run(client, "why did they fear it?", turns=turns,
                               memory={"chunk_ids": [9]}, remembered=remembered)

    assert "Turn 1 - user: Who built the press?" in client.calls[0]["user"]
    assert searches == ["Church fear of the printing press"]
    assert "Remembered passage." in client.calls[1]["user"]
    assert "Conversation so far" in client.calls[1]["user"]
    assert result["memory"]["chunk_ids"][0] == 1


def test_the_context_keeper_keeps_the_most_recent_passages():
    keeper = study.ContextKeeper(7, {"chunk_ids": [1, 2, 3]})
    keeper.remember([4, 2, None, "x"])
    assert keeper.chunk_ids == [4, 2, 1, 3]
    keeper.remember(range(10, 20))
    assert len(keeper.chunk_ids) == study.REMEMBER


# ── plan parsing ──────────────────────────────────────────────────────────────

def test_plans_drop_unknown_tools_and_clean_settings():
    plan = study.TurnPlan(steps=[{"tool": "dance"}, {"tool": "quiz", "count": "50", "difficulty": "HARD",
                                                     "scope": "chapter", "target_item": "two"}]
                          + [{"tool": "answer"}] * 6)
    assert len(plan.steps) == study.MAX_STEPS and plan.steps[0].tool == "quiz"
    q = plan.steps[0]
    assert (q.count, q.difficulty, q.scope, q.target_item) == (20, "hard", "topic", None)


def test_block_text_and_history_are_compact():
    quiz = {"type": "quiz", "items": [_quiz_item()]}
    assert block_text_has("Q1. Who built the press?", quiz)
    long = {"type": "summary", "text": "x" * 2000}
    assert len(study.block_text(long, 100)) < 120
    history = study.history_text([{"turn": n, "message": f"m{n}", "blocks": []} for n in range(1, 10)])
    assert "Turn 1 " not in history and "Turn 9 - user: m9" in history


def block_text_has(text, block):
    return text in study.block_text(block)


# ── quiz quote checks and rate-limit retries ─────────────────────────────────

def test_quiz_quotes_are_checked_against_the_cited_passage():
    items = [_quiz_item(excerpt="Gutenberg built ... around 1440"),
             _quiz_item(excerpt="Columbus sailed in 1492"),
             _quiz_item(chunk=99),
             {"question": "No citation"}]
    attach_citations(items, [P1, P2])
    assert [i.get("verified") for i in items] == [True, False, False, False]
    assert items[0]["citation"]["char_end"] == 56


def test_a_short_rate_limit_is_waited_out_once():
    class RateLimitError(Exception):
        def __init__(self):
            super().__init__("429")
            self.response = SimpleNamespace(headers={"retry-after": "2"})

    client = FakeClient(RateLimitError(), "fine")
    with patch("engine.llm_nodes.get_client", return_value=client), patch("engine.qa.time.sleep") as sleep:
        assert qa.chat_text("m", "s", "u") == ("fine", 60)
    sleep.assert_called_once_with(2.0)

    client = FakeClient(RateLimitError(), RateLimitError())
    with patch("engine.llm_nodes.get_client", return_value=client), patch("engine.qa.time.sleep"):
        with pytest.raises(qa.ModelBusy):
            qa.chat_text("m", "s", "u")


# ── sections ──────────────────────────────────────────────────────────────────

OUTLINE = [{"section": 1, "title": "Print arrives", "chunk_ids": [1, 2], "pages": [1, 2]}]


def test_a_step_can_read_one_section_of_the_outline():
    client = FakeClient(
        _plan(_step("summarize", scope="section", section=1),
              _step("quiz", count=1, scope="section", section=1)),
        "- Gutenberg and the Church",
        {"items": [_quiz_item()]},
    )
    result, events, searches = _run(client, "Summarize section 1 and quiz me on it", outline=OUTLINE)

    assert result["error"] is None
    assert "Sections of the document:" in client.calls[0]["user"]
    assert "1. Print arrives (about pages 1-2)" in client.calls[0]["user"]
    assert "Gutenberg built the printing press" in client.calls[1]["user"]
    assert "Index of Prohibited Books" in client.calls[1]["user"], "the whole section is read"
    summary, quiz = result["blocks"]
    assert summary["section"] == 1 and summary["chunk_ids"] == [1, 2]
    assert quiz["section"] == 1 and quiz["scope"] == "section"
    assert searches == [], "a named section is read directly, not searched for"
    assert [p["name"] for k, p in events if k == "step"].count("read section") == 2
    assert result["outline"] == [{"section": 1, "title": "Print arrives", "pages": [1, 2]}]


def test_an_unknown_section_falls_back_to_a_topic_search():
    client = FakeClient(_plan(_step("summarize", scope="section", section=9)), "- summary")
    result, _, searches = _run(client, "Summarize section 9", outline=OUTLINE)
    assert result["error"] is None and searches == ["printing press"]


def test_the_first_turn_maps_the_document_once():
    client = FakeClient(_plan(reply="Hi!"))
    calls = []

    def outline(document_id, build=True):
        calls.append(build)
        return None if not build else OUTLINE

    with patch("engine.progress.get_outline", side_effect=outline):
        with (patch("engine.llm_nodes.get_client", return_value=client)):
            result = study.run_turn(message="hi", document_id=7, turns=[], emit=None)
    assert calls == [False, True]
    assert [s["name"] for s in result["steps"]] == ["map sections", "plan"]
