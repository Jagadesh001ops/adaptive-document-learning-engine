"""Document Q&A: answer -> check -> re-read -> revise loop, with a scripted fake model."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from engine import qa
from engine.config import ANSWER_MODEL, CHECK_MODEL

P1 = {"chunk_id": 1, "document_id": 7, "char_start": 0, "char_end": 56,
      "text": "Gutenberg built the printing press around 1440 in Mainz."}
P2 = {"chunk_id": 2, "document_id": 7, "char_start": 50, "char_end": 140,
      "text": "The Church feared printed books spread heresy, so it issued the Index of Prohibited Books."}
P3 = {"chunk_id": 3, "document_id": 7, "char_start": 130, "char_end": 190,
      "text": "Printed books were cheaper, so far more people could read."}


def _answer(quote="feared printed books spread heresy", chunk_id=2, status="answered"):
    return {"answer": "Because it feared heresy.", "justification": "Passage 2 says so.",
            "citations": [{"chunk_id": chunk_id, "quote": quote}] if quote else [],
            "status": status, "general_knowledge": None,
            "follow_ups": ["What was on the Index?", "Who enforced it?"]}


OK = {"supported": True, "problems": [], "missing": None, "search_query": None}
BAD = {"supported": False, "problems": ["Claim not backed"], "missing": "why it was cheaper",
       "search_query": "cost of printed books"}


class FakeClient:
    """Returns scripted responses in order; each entry is a dict (JSON reply) or an Exception."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                               usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20))


def _run(client, retrieve_results):
    retrieve_calls = []

    def fake_retrieve(query, document_id, top_k):
        retrieve_calls.append({"query": query, "document_id": document_id, "top_k": top_k})
        return retrieve_results.pop(0)

    events = []
    with (
        patch("engine.llm_nodes.get_client", return_value=client),
        patch("engine.retrieval_node.retrieve_chunks", side_effect=fake_retrieve),
    ):
        result = qa.answer_question("Why did the Church ban books?", 7,
                                    emit=lambda kind, p: events.append((kind, p)))
    return result, events, retrieve_calls


def _finished_steps(events):
    return [p["name"] for kind, p in events if kind == "step"]


# ── quote checking ────────────────────────────────────────────────────────────

def test_real_quote_passes_and_formatting_differences_are_ignored():
    passage = {**P2, "text": "It's the Church's index — a list of banned books."}
    curly_apostrophe = "the church" + chr(0x2019) + "s   INDEX - a list"
    assert qa.check_quotes(_answer(quote=curly_apostrophe), [passage]) == []
    assert qa.check_quotes(_answer(quote="the church feared printed books"), [P2]) == []


def test_invented_quote_wrong_passage_and_missing_citations_are_flagged():
    assert "Quote not found" in qa.check_quotes(_answer(quote="the Pope loved books"), [P2])[0]
    assert "wasn't provided" in qa.check_quotes(_answer(chunk_id=99), [P2])[0]
    assert "cites no passages" in qa.check_quotes(_answer(quote=None), [P2])[0]
    assert qa.check_quotes(_answer(quote=None, status="not_in_document"), [P2]) == []


def test_parse_json_tolerates_reasoning_tags_and_prose():
    assert qa.parse_json('<think>hmm {no}</think>Sure: {"supported": true} done') == {"supported": True}
    with pytest.raises(ValueError):
        qa.parse_json("no json here")


# ── the loop ──────────────────────────────────────────────────────────────────

def test_answer_that_passes_the_check_is_marked_checked():
    client = FakeClient(_answer(), OK)
    result, events, retrieve_calls = _run(client, [[P1, P2]])

    assert result["verification"] == "checked" and result["revisions"] == 0
    assert result["answer"] == "Because it feared heresy."
    assert result["citations"] == [{"chunk_id": 2, "quote": "feared printed books spread heresy",
                                    "char_start": 50, "char_end": 140}]
    assert result["follow_ups"] == ["What was on the Index?", "Who enforced it?"]
    assert result["total_tokens"] == 240
    assert _finished_steps(events) == ["find passages", "answer", "check"]
    assert [c["model"] for c in client.calls] == [ANSWER_MODEL, CHECK_MODEL]
    assert retrieve_calls[0]["document_id"] == 7


def test_failed_check_rereads_new_passages_and_revises():
    client = FakeClient(_answer(quote="an invented quote"), OK, _answer(quote="far more people could read",
                                                                        chunk_id=3), OK)
    # Re-read returns P2 again (already read) and P3 (new) — only P3 should be used.
    result, events, retrieve_calls = _run(client, [[P1, P2], [P2, P3]])

    assert result["verification"] == "revised" and result["revisions"] == 1
    assert _finished_steps(events) == ["find passages", "answer", "check", "re-read", "revise", "check (2)"]
    assert retrieve_calls[1]["query"] == "Why did the Church ban books?"  # checker gave no search query
    revise_msg = client.calls[2]["messages"][1]["content"]
    assert "Quote not found" in revise_msg and "[chunk_id=3]" in revise_msg
    assert result["passages_read"] == 3


def test_reread_uses_the_checkers_search_query():
    client = FakeClient(_answer(), BAD, _answer(quote="far more people could read", chunk_id=3), OK)
    _, _, retrieve_calls = _run(client, [[P1, P2], [P3]])
    assert retrieve_calls[1]["query"] == "cost of printed books"


def test_gives_up_after_max_revisions_and_reports_problems():
    client = FakeClient(_answer(), BAD, _answer(), BAD, _answer(), BAD)
    result, events, _ = _run(client, [[P1, P2], [P3], []])

    assert result["verification"] == "unverified"
    assert result["revisions"] == qa.MAX_REVISIONS
    assert "Claim not backed" in result["problems"]
    assert _finished_steps(events)[-1] == "check (3)"


def test_check_failure_is_reported_as_unchecked_not_verified():
    client = FakeClient(_answer(), RuntimeError("judge down"))
    result, _, _ = _run(client, [[P1, P2]])
    assert result["verification"] == "unchecked"
    assert result["answer"] == "Because it feared heresy."


def test_rate_limit_becomes_model_busy():
    class RateLimitError(Exception):
        pass

    client = FakeClient(RateLimitError("429"))
    with pytest.raises(qa.ModelBusy, match="busy"):
        _run(client, [[P1, P2]])
