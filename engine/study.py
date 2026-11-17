"""Interactive study sessions: one conversation per document, any request in any message.

Each message is one *turn*:

    message -> planner -> [tool, tool, ...] -> blocks shown in the conversation

The planner (PLANNER_MODEL) reads the message with the conversation so far and composes a plan
from these tools, in any order and number:

    answer     a question about the document, with justification and quotes, checked and revised
               (engine.qa)
    summarize  a summary of the whole document or of one topic
    concepts   the key concepts, explained
    quiz       multiple-choice questions grounded in document passages, quotes checked in code
    revise     change an earlier output exactly as asked ("make question 2 harder"), nothing else

Every step carries its own settings (count, difficulty, format), so a number in one part of a
request never leaks into another. Later steps in a turn can build on earlier ones ("explain X,
then quiz me on it").

The context keeper remembers which passages the conversation has relied on, so follow-ups
("and why did that matter?") read them again alongside a fresh search. The planner also rewrites
each message as a standalone search query, resolving "that" and "it" from the conversation.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .config import ANSWER_MODEL, CHECK_MODEL, LLM_MODEL, PLANNER_MODEL
from .schemas import RequestBrief

logger = logging.getLogger(__name__)

Emit = Callable[[str, dict], None]

TOOLS = ("answer", "summarize", "concepts", "quiz", "revise")
MAX_STEPS = 4
HISTORY_TURNS = 6         # earlier turns shown to the planner
PASSAGES = 5              # passages for summarize/concepts/quiz on a topic
WHOLE_DOC_PASSAGES = 6    # passages spread across the document for a whole-document quiz
REMEMBER = 6              # passages the context keeper keeps
MAX_DOC_CHARS = 24000


# ── plan ──────────────────────────────────────────────────────────────────────

class TurnStep(BaseModel):
    tool: Literal["answer", "summarize", "concepts", "quiz", "revise"]
    instructions: str = ""
    question: Optional[str] = None          # answer: the question, standalone
    count: Optional[int] = None
    difficulty: Optional[str] = None
    format: Optional[str] = None
    length: Optional[str] = None
    scope: Literal["whole", "topic"] = "topic"
    uses_previous_step: bool = False        # build on the previous step's output in this turn
    target_turn: Optional[int] = None       # revise: which earlier turn
    target_item: Optional[int] = None       # revise: question number to change, None = all of it

    _clamp = field_validator("count", mode="before")(RequestBrief.clamp_count.__func__)
    _level = field_validator("difficulty", mode="before")(RequestBrief.normalise_difficulty.__func__)

    @field_validator("scope", mode="before")
    @classmethod
    def _scope(cls, v):
        return v if v in ("whole", "topic") else "topic"

    @field_validator("target_turn", "target_item", mode="before")
    @classmethod
    def _int_or_none(cls, v):
        try:
            return None if v in (None, "") else int(v)
        except (TypeError, ValueError):
            return None

    def brief(self) -> RequestBrief:
        return RequestBrief(count=self.count, difficulty=self.difficulty,
                            format=self.format, length=self.length)


class TurnPlan(BaseModel):
    goal: str = ""
    search_query: str = ""
    steps: List[TurnStep] = Field(default_factory=list)
    reply: Optional[str] = None             # when no tool fits: a short reply or clarifying question

    @field_validator("steps", mode="before")
    @classmethod
    def _known_tools(cls, v):
        return [s for s in (v or []) if isinstance(s, dict) and s.get("tool") in TOOLS][:MAX_STEPS]


_PLANNER_SYSTEM = """You plan one turn of a study conversation about a single document.

Tools (use only these):
- "answer": answer a question about the document (facts, why/how, comparisons, meanings,
  motives) with a justification and quotes. Use for any question or "explain ..." request.
- "summarize": summarize the whole document or one topic in it.
- "concepts": list and explain key concepts.
- "quiz": write multiple-choice questions.
- "revise": change an output from an EARLIER turn ("make question 2 harder", "shorter",
  "simpler", "replace Q3", "add an example"). Only for changes to something already produced.

Return ONLY a JSON object:
{
  "goal": "what the user wants from this turn, in one sentence",
  "search_query": "standalone search text for the topic, with words like 'that' or 'it' replaced by what they refer to",
  "steps": [
    {"tool": "answer", "instructions": "...", "question": "the question, standalone",
     "count": null, "difficulty": null, "format": null, "length": null,
     "scope": "topic", "uses_previous_step": false, "target_turn": null, "target_item": null}
  ],
  "reply": null
}

Rules:
- One message may need several steps, in the order the user asked. "Explain X, then give me 3
  hard questions on it" is an answer step followed by a quiz step with uses_previous_step true.
- Put each number, difficulty, format and length on the step it belongs to. "Summarize in 4
  bullet points and give me 3 hard questions": summarize has count 4 and format "bullet points";
  quiz has count 3 and difficulty "hard". Never copy a number from one step to another, and
  never invent settings the user didn't state.
- scope "whole" when the step is about the whole document ("summarize this", "quiz me on the
  chapter"); "topic" when it is about a specific topic.
- instructions: a direct instruction to that step carrying over what the user asked for it.
- Use the conversation to resolve references. "Quiz me on that" after an answer about the
  printing press is a quiz step on the printing press.
- revise: target_turn is the number of the earlier turn whose output should change (the latest
  turn with that output if the user doesn't say), target_item the question number when they
  name one. instructions says exactly what to change.
- If the message isn't a request about the document (greetings, thanks, unclear), return no
  steps and put a short, friendly reply or clarifying question in "reply"."""


def plan_turn(message: str, history: str) -> tuple[TurnPlan, int]:
    from .qa import _chat
    user = (f"Conversation so far:\n{history}\n\n" if history else "") + f"New message: {message}"
    data, tokens = _chat(PLANNER_MODEL, _PLANNER_SYSTEM, user, json_mode=True)
    plan = TurnPlan(**{k: v for k, v in data.items() if k in TurnPlan.model_fields})
    if not plan.search_query:
        plan.search_query = message
    return plan, tokens


# ── blocks: what a turn shows ─────────────────────────────────────────────────

def block_text(block: dict, limit: int = 0) -> str:
    """Plain-text version of a block, for the planner, for later steps and for exports."""
    kind = block.get("type")
    if kind == "answer":
        text = f"{block.get('answer', '')}\nWhy: {block.get('justification', '')}"
    elif kind == "quiz":
        lines = []
        for i, q in enumerate(block.get("items") or [], 1):
            opts = " ".join(f"{k}) {v}" for k, v in (q.get("options") or {}).items())
            lines.append(f"Q{i}. {q.get('question', '')} {opts} [answer {q.get('answer', '?')}]")
        text = "\n".join(lines)
    else:
        text = str(block.get("text") or "")
    return text[:limit] + " ..." if limit and len(text) > limit else text


def history_text(turns: List[dict]) -> str:
    """Compact view of earlier turns: what was asked and what was produced."""
    lines = []
    for t in turns[-HISTORY_TURNS:]:
        lines.append(f"Turn {t['turn']} - user: {t['message']}")
        for b in t.get("blocks") or []:
            lines.append(f"  produced {b.get('type')}:\n    " +
                         block_text(b, 600).replace("\n", "\n    "))
        if t.get("reply"):
            lines.append(f"  replied: {t['reply'][:300]}")
    return "\n".join(lines)


# ── context keeper ────────────────────────────────────────────────────────────

class ContextKeeper:
    """Remembers the passages the conversation relied on, most recent first."""

    def __init__(self, document_id: int, memory: Optional[dict] = None):
        self.document_id = document_id
        self.chunk_ids: List[int] = list((memory or {}).get("chunk_ids") or [])

    def remember(self, chunk_ids) -> None:
        for cid in reversed([c for c in chunk_ids if isinstance(c, int)]):
            if cid in self.chunk_ids:
                self.chunk_ids.remove(cid)
            self.chunk_ids.insert(0, cid)
        del self.chunk_ids[REMEMBER:]

    def passages(self) -> List[dict]:
        return load_chunks(self.document_id, self.chunk_ids)

    def to_json(self) -> dict:
        return {"chunk_ids": self.chunk_ids}


def _chunk_dict(c) -> dict:
    return {"chunk_id": c.id, "document_id": c.document_id, "text": c.text,
            "char_start": c.char_start, "char_end": c.char_end}


def load_chunks(document_id: int, chunk_ids: List[int]) -> List[dict]:
    if not chunk_ids:
        return []
    from .database import DocumentChunk, SessionLocal
    db = SessionLocal()
    try:
        rows = (db.query(DocumentChunk)
                .filter(DocumentChunk.document_id == document_id, DocumentChunk.id.in_(chunk_ids))
                .all())
    finally:
        db.close()
    by_id = {r.id: _chunk_dict(r) for r in rows}
    return [by_id[c] for c in chunk_ids if c in by_id]


def spread_chunks(document_id: int, n: int) -> List[dict]:
    """n passages spread evenly through the document, for whole-document quizzes."""
    from .database import DocumentChunk, SessionLocal
    db = SessionLocal()
    try:
        rows = (db.query(DocumentChunk).filter_by(document_id=document_id)
                .order_by(DocumentChunk.chunk_index).all())
    finally:
        db.close()
    if len(rows) > n:
        step = len(rows) / n
        rows = [rows[int(i * step)] for i in range(n)]
    return [_chunk_dict(r) for r in rows]


def document_text(document_id: int, max_chars: int = MAX_DOC_CHARS) -> str:
    from .chunking import join_chunks
    from .database import DocumentChunk, SessionLocal
    db = SessionLocal()
    try:
        rows = (db.query(DocumentChunk).filter_by(document_id=document_id)
                .order_by(DocumentChunk.chunk_index).all())
    finally:
        db.close()
    return join_chunks(rows)[:max_chars]


# ── turn runner ───────────────────────────────────────────────────────────────

class Turn:
    """Runs one planned turn, emitting progress and collecting what the reviewer needs."""

    def __init__(self, *, message: str, document_id: int, turns: List[dict], keeper: ContextKeeper,
                 emit: Optional[Emit] = None):
        self.message = message
        self.document_id = document_id
        self.turns = turns
        self.keeper = keeper
        self.emit = emit or (lambda *_: None)
        self.history = history_text(turns)
        self.blocks: List[dict] = []
        self.steps: List[dict] = []
        self.passages: dict = {}         # every passage read this turn, by chunk_id
        self.sources: dict = {}          # whole-document text, when a step used it
        self.targets: List[dict] = []    # earlier outputs a revise step changed
        self.tokens = 0
        self._search_cache: dict = {}

    # progress -----------------------------------------------------------------
    def _record(self, kind, payload):
        if kind == "step":
            self.steps.append(payload)
        self.emit(kind, payload)

    def step(self, name: str, fn, detail=lambda r: None):
        from .qa import _step
        result, tokens = _step(self._record, name, fn, detail)
        self.tokens += tokens
        return result

    # passages -----------------------------------------------------------------
    def _read(self, passages: List[dict]) -> List[dict]:
        for p in passages:
            self.passages.setdefault(p["chunk_id"], p)
        return passages

    def topic_passages(self, query: str) -> List[dict]:
        if query not in self._search_cache:
            def find():
                from .retrieval_node import retrieve_chunks
                found = retrieve_chunks(query=query, document_id=self.document_id, top_k=PASSAGES)
                seen = {p["chunk_id"] for p in found}
                found += [p for p in self.keeper.passages() if p["chunk_id"] not in seen][:2]
                return found, 0
            self._search_cache[query] = self.step("find passages", find, lambda r: f"{len(r)} passages")
        return self._read(self._search_cache[query])

    # tools --------------------------------------------------------------------
    def run(self, plan: TurnPlan) -> None:
        previous: Optional[dict] = None
        for i, step in enumerate(plan.steps):
            tool = getattr(self, f"_{step.tool}")
            block = tool(step, plan, previous if step.uses_previous_step else None)
            if block is None:
                continue
            block["step"] = i
            self.blocks.append(block)
            self.emit("block", {"block": block})   # wrapped: a block has its own "type"
            previous = block

    def _answer(self, step: TurnStep, plan: TurnPlan, previous: Optional[dict]) -> dict:
        from .qa import answer_question
        question = step.question or self.message
        history = self.history
        if previous:
            history += f"\n(Earlier in this turn: {block_text(previous, 800)})"
        result = answer_question(question, self.document_id, self._record,
                                 search_query=plan.search_query, history=history,
                                 extra_passages=self.keeper.passages())
        self.tokens += result.pop("total_tokens", 0)
        for p in result.pop("passages", []):
            self.passages.setdefault(p["chunk_id"], p)
        result.pop("models", None)
        return {"type": "answer", **result}

    def _worker_input(self, step: TurnStep, plan: TurnPlan, previous: Optional[dict]):
        if step.scope == "whole" and not previous:
            text = document_text(self.document_id)
            self.sources["document"] = text
            return text, []
        passages = self.topic_passages(plan.search_query)
        text = "\n\n".join(p["text"] for p in passages)
        if previous:
            text = f"{block_text(previous)}\n\nSource passages:\n{text}"
        return text, passages

    def _text_tool(self, node_class, kind: str, step: TurnStep, plan: TurnPlan,
                   previous: Optional[dict]) -> dict:
        from .llm_nodes import StepContext
        from .qa import chat_text
        source, passages = self._worker_input(step, plan, previous)
        node = node_class(name=kind, input_key="document", output_key=kind)
        node.set_context(StepContext(request=self.message, goal=plan.goal,
                                     instructions=step.instructions, brief=step.brief()))
        system, user = node._build_messages({"document": source})
        text = self.step(kind, lambda: chat_text(LLM_MODEL, system, user),
                         lambda r: f"{len(r.split())} words")
        self.keeper.remember([p["chunk_id"] for p in passages])
        return {"type": kind, "text": text.strip(), "scope": step.scope}

    def _summarize(self, step, plan, previous):
        from .llm_nodes import SummarizeNode
        return self._text_tool(SummarizeNode, "summary", step, plan, previous)

    def _concepts(self, step, plan, previous):
        from .llm_nodes import ExtractConceptsNode
        return self._text_tool(ExtractConceptsNode, "concepts", step, plan, previous)

    def _quiz(self, step: TurnStep, plan: TurnPlan, previous: Optional[dict]) -> dict:
        from .llm_nodes import GenerateQuizNode, StepContext, attach_citations
        from .qa import chat_text, parse_json
        if step.scope == "whole" and not previous:
            passages = self.step("find passages", lambda: (spread_chunks(self.document_id, WHOLE_DOC_PASSAGES), 0),
                                 lambda r: f"{len(r)} passages across the document")
            self._read(passages)
        else:
            passages = self.topic_passages(plan.search_query)
        if not passages:
            raise ValueError("No passages found in the document to write questions from.")
        instructions = step.instructions
        if previous:
            instructions += ("\nBase the questions on this material from earlier in the turn, "
                             f"using the passages as the source:\n{block_text(previous, 1500)}")
        node = GenerateQuizNode(name="quiz", input_key="document", output_key="quiz")
        node.set_context(StepContext(request=self.message, goal=plan.goal,
                                     instructions=instructions, brief=step.brief()))
        system, user = node._build_messages({"retrieved_chunks": passages, "document": ""})
        count = node._settings()[0]

        def write():
            text, tokens = chat_text(LLM_MODEL, system + '\nWrap the array as {"items": [...]}.',
                                     user, json_mode=True)
            data = parse_json(text)
            items = data.get("items") if isinstance(data, dict) else None
            items = [q for q in (items or []) if isinstance(q, dict) and q.get("question")][:count]
            attach_citations(items, passages)
            return items, tokens

        items = self.step("quiz", write, lambda r: f"{len(r)} questions")
        self.keeper.remember([(q.get("citation") or {}).get("chunk_id") for q in items])
        return {"type": "quiz", "items": items, "difficulty": node._settings()[1]}

    def _revise(self, step: TurnStep, plan: TurnPlan, previous: Optional[dict]) -> dict:
        from .qa import _chat
        target_turn, block = self._find_target(step, previous)
        if block is None:
            return {"type": "note", "text": "There's nothing earlier to change yet."}
        item_no = step.target_item if block["type"] == "quiz" else None
        items = block.get("items") or []
        if item_no is not None and not 1 <= item_no <= len(items):
            return {"type": "note", "text": f"There's no question {item_no} to change."}
        self.targets.append({"turn": target_turn, "item": item_no, "block": block})

        if block["type"] == "quiz":
            original = items[item_no - 1] if item_no else items
            shape = ('{"item": {...one question in the same format...}}' if item_no
                     else '{"items": [...questions in the same format...]}')
        elif block["type"] == "answer":
            original = {"answer": block.get("answer"), "justification": block.get("justification")}
            shape = '{"answer": "...", "justification": "..."}'
        else:
            original = {"text": block.get("text")}
            shape = '{"text": "..."}'

        cited = [(q.get("citation") or {}).get("chunk_id") for q in items] if items else \
                [c.get("chunk_id") for c in block.get("citations") or []]
        # The passages the original relied on, plus a fresh search: a harder question or a fuller
        # answer may need material the original didn't use.
        passages = self._read(load_chunks(self.document_id, [c for c in cited if c]))
        seen = {p["chunk_id"] for p in passages}
        passages += [p for p in self.topic_passages(plan.search_query) if p["chunk_id"] not in seen]
        source = "\n\n".join(f"[chunk_id={p['chunk_id']}]\n{p['text']}" for p in passages)
        system = ("You revise an earlier output for a student exactly as they ask. Make the change "
                  "they asked for and keep everything else the same. Stay grounded in the source "
                  "passages; don't add outside facts. For quiz questions keep the JSON fields "
                  "(question, options A-D, answer, explanation, citation with chunk_id and an "
                  f"excerpt copied exactly from the passage). Return ONLY JSON: {shape}")
        user = (f"The student's message: {self.message}\nChange to make: {step.instructions}\n\n"
                f"Earlier output:\n{json.dumps(original, ensure_ascii=False)}\n\nSource passages:\n{source}")
        data = self.step("revise", lambda: _chat(ANSWER_MODEL, system, user, json_mode=True),
                         lambda r: "revised")

        revised_from = {"turn": target_turn, "item": item_no}
        if block["type"] == "quiz":
            from .llm_nodes import attach_citations
            if item_no:
                new_item = data.get("item") if isinstance(data.get("item"), dict) else None
                if not new_item:
                    raise ValueError("The revision didn't return a question.")
                attach_citations([new_item], passages)
                new_items = [dict(q) for q in items]
                new_items[item_no - 1] = new_item          # only the question the user named
            else:
                new_items = [q for q in data.get("items") or [] if isinstance(q, dict)]
                attach_citations(new_items, passages)
            return {"type": "quiz", "items": new_items, "difficulty": block.get("difficulty"),
                    "revised_from": revised_from}
        if block["type"] == "answer":
            return {**block, "answer": data.get("answer") or block.get("answer"),
                    "justification": data.get("justification") or block.get("justification"),
                    "verification": "unchecked", "revised_from": revised_from}
        return {"type": block["type"], "text": data.get("text") or block.get("text"),
                "scope": block.get("scope"), "revised_from": revised_from}

    def _find_target(self, step: TurnStep, previous: Optional[dict]):
        if previous:
            return None, previous
        wanted = "quiz" if step.target_item else None
        candidates = [t for t in self.turns if t.get("blocks")]
        if step.target_turn is not None:
            candidates = [t for t in candidates if t["turn"] == step.target_turn] or candidates
        for t in reversed(candidates):
            blocks = [b for b in t["blocks"] if b.get("type") in ("answer", "summary", "concepts", "quiz")]
            if wanted:
                blocks = [b for b in blocks if b["type"] == wanted]
            if blocks:
                return t["turn"], blocks[-1]
        return None, None


def run_turn(*, message: str, document_id: int, turns: List[dict], memory: Optional[dict] = None,
             emit: Optional[Emit] = None) -> dict:
    """Plan and run one turn. Always returns a result; failures are reported in "error"."""
    from .qa import ModelBusy
    emit = emit or (lambda *_: None)
    keeper = ContextKeeper(document_id, memory)
    turn = Turn(message=message, document_id=document_id, turns=turns, keeper=keeper, emit=emit)
    plan, error = None, None
    t0 = time.perf_counter()
    try:
        plan = turn.step("plan", lambda: plan_turn(message, turn.history),
                         lambda p: " -> ".join(s.tool for s in p.steps) or "reply")
        emit("plan", {"goal": plan.goal, "steps": [s.model_dump() for s in plan.steps]})
        if plan.steps:
            turn.run(plan)
        elif plan.reply:
            emit("reply", {"text": plan.reply})
    except ModelBusy as exc:
        error = str(exc)
    except Exception as exc:
        logger.exception("study turn failed")
        error = f"Something went wrong: {exc}"

    for b in turn.blocks:
        keeper.remember([c.get("chunk_id") for c in b.get("citations") or []])
    return {
        "message": message,
        "goal": plan.goal if plan else None,
        "plan": plan.model_dump() if plan else None,
        "reply": plan.reply if plan and not plan.steps else None,
        "blocks": turn.blocks,
        "steps": turn.steps,
        "error": error,
        "passages": list(turn.passages.values()),
        "sources": turn.sources,
        "targets": turn.targets,
        "memory": keeper.to_json(),
        "total_tokens": turn.tokens,
        "time_ms": round((time.perf_counter() - t0) * 1000),
        "models": {"planner": PLANNER_MODEL, "steps": LLM_MODEL, "answer": ANSWER_MODEL,
                   "check": CHECK_MODEL},
    }
