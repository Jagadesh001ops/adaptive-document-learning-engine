from dotenv import load_dotenv

load_dotenv()

from dataclasses import dataclass, field
from typing import Any, Dict

from .config import LLM_MODEL
from .nodes import BaseNode, NodeResult
from .registry import NodeEntry, NodeRegistry, _set_registry
from .schemas import RequestBrief

client = None


def get_client():
    global client
    if client is None:
        from groq import Groq
        client = Groq()
    return client


@dataclass
class StepContext:
    """What the user asked for, given to every step so its output fits the request."""
    request: str = ""
    goal: str = ""
    instructions: str = ""
    brief: RequestBrief = field(default_factory=RequestBrief)


# ─── Generic fallback node ────────────────────────────────────────────────────

class LLMNode(BaseNode):
    """Dynamic node — behaviour driven entirely by config, not class definition."""

    def __init__(self, name: str, description: str, input_key: str, output_key: str):
        super().__init__(name=name, description=description)
        self.input_key = input_key
        self.output_key = output_key
        self.context = StepContext()

    def set_context(self, context: StepContext) -> None:
        self.context = context

    def _context_block(self) -> str:
        ctx = self.context
        lines = []
        if ctx.request:
            lines.append(f'The user\'s request: "{ctx.request}"')
        if ctx.goal:
            lines.append(f"Overall goal: {ctx.goal}")
        if ctx.instructions and ctx.instructions != self.description:
            lines.append(f"Your part: {ctx.instructions}")
        prefs = ctx.brief.preferences()
        if prefs:
            lines.append("Follow these preferences where they apply to your output:")
            lines.extend(f"- {k}: {v}" for k, v in prefs.items())
        if lines:
            # Earlier/later steps handle the rest of a multi-part request.
            lines.append("Do only your part of the request.")
        return "\n".join(lines)

    def _system_prompt(self, role: str) -> str:
        block = self._context_block()
        return f"{role}\n\n{block}" if block else role

    def _build_messages(self, state: Dict[str, Any]):
        text = state.get(self.input_key)
        if not text:
            return None, None
        return (
            self._system_prompt(f"You are an expert assistant. Your job: {self.description}. Be clear."),
            str(text),
        )

    def _execute(self, state: Dict[str, Any]) -> NodeResult:
        system_msg, user_msg = self._build_messages(state)
        if system_msg is None:
            return self.handle_failure(
                ValueError(f"'{self.input_key}' not found in state")
            )

        response = get_client().chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user",   "content": user_msg},
            ]
        )

        output = response.choices[0].message.content
        return NodeResult(
            success=True,
            output_key=self.output_key,
            output_value=output,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
        )


# ─── Specialist nodes ─────────────────────────────────────────────────────────

class SummarizeNode(LLMNode):
    def __init__(self, name: str, input_key: str, output_key: str):
        super().__init__(
            name=name,
            description=(
                "Summarize the given text, capturing only the main ideas. Use one concise "
                "paragraph unless the user asked for a different format or length"
            ),
            input_key=input_key,
            output_key=output_key,
        )


class ExtractConceptsNode(LLMNode):
    def __init__(self, name: str, input_key: str, output_key: str):
        super().__init__(
            name=name,
            description=(
                "Extract the most important concepts from the text (5 unless the user asked "
                "for a different number). Return a numbered list, one concept per line with a "
                "brief explanation, unless the user asked for a different format"
            ),
            input_key=input_key,
            output_key=output_key,
        )


class GenerateQuizNode(LLMNode):
    """Generates quiz items as structured JSON, attaching citations from retrieved_chunks."""

    DEFAULT_COUNT = 5
    _LEVELS = {
        "easy": "recall of facts stated directly in the text",
        "medium": "understanding of ideas and how they connect",
        "hard": "applying or inferring from the text, with plausible distractors",
    }

    def __init__(self, name: str, input_key: str, output_key: str):
        super().__init__(
            name=name,
            description="Create multiple-choice quiz questions based on the given content.",
            input_key=input_key,
            output_key=output_key,
        )
        self._difficulty = None  # UI-selected level; the user's own request takes precedence

    def _settings(self):
        brief = self.context.brief
        count = brief.count or self.DEFAULT_COUNT
        difficulty = brief.difficulty or self._difficulty or "medium"
        return count, difficulty if difficulty in self._LEVELS else "medium"

    def _build_messages(self, state: Dict[str, Any]):
        text = state.get(self.input_key)
        chunks = state.get("retrieved_chunks") or []

        # Build source context: prefer retrieved chunks, fall back to raw text
        if chunks:
            source = "\n\n---\n\n".join(
                f"[chunk_id={c.get('chunk_id')}]\n{c.get('text', '')}"
                for c in chunks[:8]
            )
        elif text:
            source = str(text)
        else:
            return None, None

        count, difficulty = self._settings()
        role = (
            "You are an expert quiz writer. Given source material (with chunk IDs), "
            f"generate exactly {count} multiple-choice questions at {difficulty} difficulty "
            f"({self._LEVELS[difficulty]}). Every question and answer must be supported by "
            "the source material — do not rely on outside knowledge. For each question cite "
            "the chunk_id of the chunk it came from, with a short excerpt copied exactly from "
            "that chunk. "
            "Return ONLY a JSON array — no extra text:\n"
            '[\n'
            '  {\n'
            '    "question": "...",\n'
            '    "options": {"A": "...", "B": "...", "C": "...", "D": "..."},\n'
            '    "answer": "A",\n'
            '    "explanation": "One sentence explaining why the answer is correct.",\n'
            '    "citation": {"chunk_id": 42, "excerpt": "short quoted phrase"}\n'
            '  }\n'
            ']'
        )
        return self._system_prompt(role), source

    def _execute(self, state: Dict[str, Any]) -> NodeResult:
        system_msg, user_msg = self._build_messages(state)
        if system_msg is None:
            return self.handle_failure(ValueError(f"'{self.input_key}' not found in state"))

        response = get_client().chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user",   "content": user_msg},
            ],
        )
        raw = response.choices[0].message.content
        # Try to parse; store as list if valid JSON, else raw string
        import json as _json
        try:
            output = _json.loads(raw)
        except _json.JSONDecodeError:
            output = raw

        if isinstance(output, list):
            # The requested number is a rule, not a suggestion.
            output = [item for item in output if isinstance(item, dict)][:self._settings()[0]]
            attach_citations(output, state.get("retrieved_chunks") or [])

        return NodeResult(
            success=True,
            output_key=self.output_key,
            output_value=output,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
        )


def attach_citations(items: list, chunks: list) -> None:
    """Add source positions to each quiz item's citation and check its quote is really in that chunk.

    Items whose quote can't be found are marked verified=False with a note, never dropped."""
    from .qa import quote_in
    chunk_map = {c.get("chunk_id"): c for c in chunks}
    for item in items:
        cit = item.get("citation")
        if not isinstance(cit, dict) or cit.get("chunk_id") not in chunk_map:
            if chunks:
                item["verified"] = False
                item["verification_note"] = "No source passage cited."
            continue
        chunk = chunk_map[cit["chunk_id"]]
        cit["document_id"] = chunk.get("document_id")
        cit["char_start"] = chunk.get("char_start")
        cit["char_end"] = chunk.get("char_end")
        # Excerpts may skip words with an ellipsis; each piece must appear in the passage.
        excerpt = (cit.get("excerpt") or "").replace(chr(0x2026), "...")
        parts = [p.strip() for p in excerpt.split("...") if p.strip()]
        if parts and all(quote_in(p, chunk.get("text", "")) for p in parts):
            item.setdefault("verified", True)
        else:
            item["verified"] = False
            item["verification_note"] = "The quoted excerpt isn't in the cited passage."


class RetrieveChunksNode(BaseNode):
    """Retrieval node — embeds the query and pulls top-N chunks from pgvector.

    Reads `query` (or falls back to `document`) from state; writes
    `retrieved_chunks` — a list of {chunk_id, text, char_start, char_end,
    document_id, score} dicts ordered by relevance.
    """

    def __init__(self, name: str, input_key: str = "query",
                 output_key: str = "retrieved_chunks",
                 document_id: int | None = None, top_k: int = 5):
        super().__init__(
            name=name,
            description="Retrieve the most relevant chunks from the document store for the given query.",
        )
        self.input_key = input_key
        self.output_key = output_key
        self.document_id = document_id
        self.top_k = top_k
        self.require_document = False

    def _execute(self, state: Dict[str, Any]) -> NodeResult:
        from .retrieval_node import retrieve_chunks
        query = state.get(self.input_key) or state.get("document", "")
        if not query:
            return self.handle_failure(ValueError("No query text found in state"))
        if self.require_document and self.document_id is None:
            # Unscoped search would cover every user's documents.
            return self.handle_failure(ValueError("No document selected to search"))

        chunks = retrieve_chunks(
            query=str(query),
            document_id=self.document_id,
            top_k=self.top_k,
        )
        return NodeResult(
            success=True,
            output_key=self.output_key,
            output_value=chunks,
            input_tokens=0,
            output_tokens=0,
        )


# ─── Registry setup ───────────────────────────────────────────────────────────

def _build_registry() -> NodeRegistry:
    r = NodeRegistry()
    r.register(NodeEntry(
        name="summarize",
        description="Summarise source text into a concise paragraph of key ideas.",
        node_class=SummarizeNode,
        aliases=["summarize_document", "summary", "summarise", "summarise_document"],
        prefixes=["summarize_", "summary_", "summarise_"],
    ))
    r.register(NodeEntry(
        name="extract_concepts",
        description="Extract the 5 most important concepts from the text as a numbered list.",
        node_class=ExtractConceptsNode,
        aliases=["extract", "extract_key_concepts"],
        prefixes=["extract_"],
    ))
    r.register(NodeEntry(
        name="generate_quiz",
        description="Generate 5 multiple-choice quiz questions with answers and explanations from the source text.",
        node_class=GenerateQuizNode,
        aliases=["quiz", "generate_quiz_questions", "create_quiz"],
        prefixes=["quiz_", "generate_quiz_", "create_quiz_"],
    ))
    r.register(NodeEntry(
        name="retrieve_chunks",
        description="Retrieve the most relevant text chunks from the document store for the given query.",
        node_class=RetrieveChunksNode,
        aliases=["retrieve", "retrieval"],
        prefixes=["retrieve_"],
    ))
    from .verification_node import VerifyQuizNode
    r.register(NodeEntry(
        name="verify_quiz",
        description="Verify each quiz answer against source chunks using an LLM judge; regenerate wrong answers once.",
        node_class=VerifyQuizNode,
        aliases=["verify", "verify_answers"],
        prefixes=["verify_"],
    ))
    return r


_set_registry(_build_registry())


# ─── Public builder ───────────────────────────────────────────────────────────

def build_node_for_step(name: str, description: str,
                         input_key: str, output_key: str) -> BaseNode:
    from .registry import get_registry
    from .verification_node import VerifyQuizNode
    NodeClass = get_registry().resolve(name)

    if NodeClass is RetrieveChunksNode:
        return RetrieveChunksNode(name=name, input_key=input_key, output_key=output_key)

    if NodeClass is VerifyQuizNode:
        return VerifyQuizNode(name=name, input_key=input_key, output_key=output_key)

    if NodeClass is not None and NodeClass is not LLMNode:
        return NodeClass(name=name, input_key=input_key, output_key=output_key)

    print(f"  -> No specialist for '{name}', using generic LLMNode")
    return LLMNode(
        name=name,
        description=description,
        input_key=input_key,
        output_key=output_key,
    )
