"""Question answering over one document, with a self-check loop.

find passages -> answer -> check -> (re-read -> revise -> check) x MAX_REVISIONS

The check has two parts: code confirms every quote really appears in the passage
it cites, then a second model (CHECK_MODEL) judges whether the passages support
the answer and says what's missing. A failed check triggers a targeted re-read
(new passages only) and a revision based on the critique.
"""
from __future__ import annotations

import json
import logging
import re
import time
from functools import partial
from typing import Callable, List, Optional

from .config import ANSWER_MODEL, CHECK_MODEL

logger = logging.getLogger(__name__)

TOP_K = 4          # passages for the first answer (~600 tokens each)
REREAD_K = 3       # max new passages per re-read
MAX_REVISIONS = 2

Emit = Callable[[str, dict], None]


class ModelBusy(Exception):
    """The LLM provider rate-limited us."""


_ANSWER_SYSTEM = """You answer questions about a document using only the passages provided.

Return ONLY a JSON object:
{
  "answer": "direct answer to the question",
  "justification": "how the passages support the answer, in plain words",
  "citations": [{"chunk_id": 12, "quote": "exact words copied from that passage"}],
  "status": "answered" | "partial" | "not_in_document",
  "general_knowledge": null,
  "follow_ups": ["question", "question", "question"]
}

Rules:
- Base the answer only on the passages. Each quote must be copied exactly, character for
  character, from the passage with that chunk_id. Keep quotes short (a sentence or less).
- Any kind of question is fine: facts, why/how, comparisons, definitions, or reasoning about
  motives ("why would someone do this"). When you infer rather than read, say so in the
  justification.
- If the passages don't answer the question, set status to "not_in_document", say what they
  do cover, and don't guess.
- Outside knowledge goes only in general_knowledge (otherwise null), never in answer.
- follow_ups: 2-3 short questions a curious reader might ask next about this document."""

_REVISE_NOTE = """

Your previous answer failed a check. Fix every problem listed, using the passages below
(some may be new). Return the full JSON object again."""

_CHECK_SYSTEM = """You check an answer to a question about a document against the passages it was based on.

Return ONLY a JSON object:
{"supported": true, "problems": [], "missing": null, "search_query": null}

- supported is true only if every claim in "answer" is backed by the passages. Inferences are
  acceptable when the justification labels them and they follow from the passages.
- An answer with status "not_in_document" is supported if the passages really don't answer it.
- Ignore "general_knowledge" and "follow_ups".
- If unsupported: list the problems, describe what information is missing, and give a short
  search_query that would find it in the document."""


# ── helpers ───────────────────────────────────────────────────────────────────

# Curly quotes and en/em dashes -> ASCII, so quotes copied by the model still match.
_PUNCT = str.maketrans({0x201C: '"', 0x201D: '"', 0x2018: "'", 0x2019: "'",
                        0x2013: "-", 0x2014: "-"})


def _norm(s: str) -> str:
    s = s.lower().translate(_PUNCT)
    return re.sub(r"\s+", " ", s).strip()


def parse_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating reasoning tags and surrounding prose."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("model did not return a JSON object")
    return json.loads(text[start:end + 1])


def check_quotes(answer: dict, passages: List[dict]) -> List[str]:
    """Problems with the answer's citations; empty when every quote is real."""
    by_id = {p["chunk_id"]: p for p in passages}
    problems = []
    citations = answer.get("citations") or []
    for c in citations:
        cid, quote = c.get("chunk_id"), (c.get("quote") or "").strip()
        if cid not in by_id:
            problems.append(f"Citation points to passage {cid}, which wasn't provided.")
        elif not quote or _norm(quote) not in _norm(by_id[cid]["text"]):
            problems.append(f'Quote not found in passage {cid}: "{quote[:100]}"')
    if answer.get("status") in ("answered", "partial") and not citations:
        problems.append("The answer cites no passages.")
    return problems


def _format_passages(passages: List[dict]) -> str:
    return "\n\n".join(f"[chunk_id={p['chunk_id']}]\n{p['text']}" for p in passages)


def _chat(model: str, system: str, user: str, json_mode: bool = False):
    from .llm_nodes import get_client
    kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
    try:
        resp = get_client().chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            **kwargs,
        )
    except Exception as exc:
        if type(exc).__name__ == "RateLimitError":
            raise ModelBusy("The model is busy (free-tier rate limit). Try again in a minute.") from exc
        raise
    usage = getattr(resp, "usage", None)
    tokens = (usage.prompt_tokens + usage.completion_tokens) if usage else 0
    return parse_json(resp.choices[0].message.content), tokens


def _step(emit: Emit, name: str, fn, detail=lambda r: None):
    emit("step_start", {"name": name})
    t0 = time.perf_counter()
    try:
        result, tokens = fn()
    except Exception as exc:
        emit("step", {"name": name, "success": False, "time_ms": round((time.perf_counter() - t0) * 1000),
                      "tokens": 0, "error": str(exc)})
        raise
    emit("step", {"name": name, "success": True, "time_ms": round((time.perf_counter() - t0) * 1000),
                  "tokens": tokens, "detail": detail(result)})
    return result, tokens


def _new_passages(retrieve, query: str, document_id: int, already_read: set):
    found = retrieve(query=query, document_id=document_id, top_k=TOP_K + REREAD_K)
    return [c for c in found if c["chunk_id"] not in already_read][:REREAD_K], 0


# ── main loop ─────────────────────────────────────────────────────────────────

def answer_question(question: str, document_id: int, emit: Optional[Emit] = None) -> dict:
    from .retrieval_node import retrieve_chunks
    emit = emit or (lambda *_: None)
    total = 0

    passages, _ = _step(emit, "find passages",
                        lambda: (retrieve_chunks(query=question, document_id=document_id, top_k=TOP_K), 0),
                        lambda r: f"{len(r)} passages")
    read = {p["chunk_id"]: p for p in passages}   # every passage read so far
    current = list(passages)                      # passages the current answer is based on

    first_msg = f"Question: {question}\n\nPassages:\n{_format_passages(current)}"
    answer, t = _step(emit, "answer", partial(_chat, ANSWER_MODEL, _ANSWER_SYSTEM, first_msg, json_mode=True),
                      lambda r: r.get("status"))
    total += t

    verification, problems, revisions = "unchecked", [], 0
    for attempt in range(MAX_REVISIONS + 1):
        problems = check_quotes(answer, current)
        check_name = "check" if attempt == 0 else f"check ({attempt + 1})"
        shown = json.dumps({k: answer.get(k) for k in ("answer", "justification", "citations", "status")})
        check_msg = f"Question: {question}\n\nAnswer:\n{shown}\n\nPassages:\n{_format_passages(current)}"
        try:
            verdict, t = _step(emit, check_name, partial(_chat, CHECK_MODEL, _CHECK_SYSTEM, check_msg),
                               lambda r: "supported" if r.get("supported") else "problems found")
            total += t
        except Exception as exc:
            # Fail closed: an answer we couldn't check is reported as unchecked, never as verified.
            logger.warning("answer check failed: %s", exc)
            verification = "unchecked"
            break

        if not problems and verdict.get("supported"):
            verification = "checked" if attempt == 0 else "revised"
            break
        problems += [p for p in (verdict.get("problems") or []) if p]
        if attempt == MAX_REVISIONS:
            verification = "unverified"
            break

        query = verdict.get("search_query") or verdict.get("missing") or question
        n = attempt + 1
        new, _ = _step(emit, f"re-read ({n})" if n > 1 else "re-read",
                       partial(_new_passages, retrieve_chunks, query, document_id, set(read)),
                       lambda r: f"{len(r)} new passages")
        read.update((c["chunk_id"], c) for c in new)
        # Revise from what the answer relied on plus the new passages, not everything read so far.
        cited = {c.get("chunk_id") for c in answer.get("citations") or []}
        current = ([p for p in current if p["chunk_id"] in cited] + new) or current

        previous = json.dumps({k: answer.get(k) for k in ("answer", "justification", "citations", "status")})
        issues = "\n".join(f"- {p}" for p in problems)
        revise_msg = (f"Question: {question}\n\nPrevious answer:\n{previous}\n\n"
                      f"Problems:\n{issues}\n\nPassages:\n{_format_passages(current)}")
        answer, t = _step(emit, f"revise ({n})" if n > 1 else "revise",
                          partial(_chat, ANSWER_MODEL, _ANSWER_SYSTEM + _REVISE_NOTE, revise_msg, json_mode=True),
                          lambda r: r.get("status"))
        total += t
        revisions = n

    citations = []
    for c in answer.get("citations") or []:
        p = read.get(c.get("chunk_id"), {})
        citations.append({"chunk_id": c.get("chunk_id"), "quote": c.get("quote"),
                          "char_start": p.get("char_start"), "char_end": p.get("char_end")})

    return {
        "question": question,
        "answer": answer.get("answer") or "",
        "justification": answer.get("justification") or "",
        "citations": citations,
        "status": answer.get("status") or "answered",
        "general_knowledge": answer.get("general_knowledge"),
        "follow_ups": [q for q in (answer.get("follow_ups") or []) if isinstance(q, str)][:3],
        "verification": verification,
        "revisions": revisions,
        "problems": problems if verification == "unverified" else [],
        "passages_read": len(read),
        "passages": [{"chunk_id": p["chunk_id"], "text": p["text"]} for p in read.values()],
        "models": {"answer": ANSWER_MODEL, "check": CHECK_MODEL},
        "total_tokens": total,
    }
