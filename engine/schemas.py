from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

_DIFFICULTIES = ("easy", "medium", "hard")


class RequestBrief(BaseModel):
    """Preferences the user stated in their request. Unstated fields stay None."""
    format: Optional[str] = None      # e.g. "bullet points", "table"
    length: Optional[str] = None      # e.g. "two sentences", "detailed"
    count: Optional[int] = None       # number of items, e.g. quiz questions
    difficulty: Optional[str] = None  # easy | medium | hard
    focus: Optional[str] = None       # part or topic of the document
    audience: Optional[str] = None    # e.g. "a 10-year-old", "exam revision"
    notes: Optional[str] = None       # anything else the user asked for

    @field_validator("count", mode="before")
    @classmethod
    def clamp_count(cls, v):
        try:
            return None if v is None else min(max(int(v), 1), 20)
        except (TypeError, ValueError):
            return None

    @field_validator("difficulty", mode="before")
    @classmethod
    def normalise_difficulty(cls, v):
        v = str(v).strip().lower() if v else None
        return v if v in _DIFFICULTIES else None

    def preferences(self) -> dict:
        return self.model_dump(exclude_none=True)


class WorkflowStep(BaseModel):
    name: str = Field(description="snake_case step name e.g. summarize_document")
    description: str = Field(
        description="Instruction for this step, tailored to the user's request"
    )
    input_key: str = Field(description="State key this step reads from")
    output_key: str = Field(description="State key this step writes to")


class WorkflowPlan(BaseModel):
    goal: str = Field(description="The user's intent in one clear sentence")
    steps: List[WorkflowStep] = Field(description="Ordered list of workflow steps")
    estimated_steps: int = Field(ge=1, le=10, description="Total number of steps")
    scope: Literal["single_document", "multi_document", "library"] = Field(
        default="single_document",
        description="Scope of document access required for this plan",
    )
    too_broad: bool = Field(
        default=False,
        description="True when the request is too broad to plan reliably",
    )
    too_broad_reason: Optional[str] = Field(
        default=None,
        description="Human-readable explanation of why the request is too broad",
    )
    brief: RequestBrief = Field(
        default_factory=RequestBrief,
        description="Preferences stated in the request, passed to every step",
    )

    @field_validator("brief", mode="before")
    @classmethod
    def brief_defaults(cls, v):
        return v or {}

    @model_validator(mode="after")
    def estimated_steps_matches_steps(self):
        if self.estimated_steps != len(self.steps):
            raise ValueError("estimated_steps must match the number of steps")
        return self


# ─── Citation & quiz output schemas ──────────────────────────────────────────

class Citation(BaseModel):
    chunk_id: Optional[int] = None
    document_id: Optional[int] = None
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    excerpt: Optional[str] = None   # short quoted text from the source chunk


class QuizOption(BaseModel):
    A: str
    B: str
    C: str
    D: str


class QuizItem(BaseModel):
    question: str
    options: QuizOption
    answer: Literal["A", "B", "C", "D"]
    explanation: str
    citation: Optional[Citation] = None
    verified: Optional[bool] = None          # set by Day 10 verification pass
    verification_note: Optional[str] = None  # set when verified=False


class QuizOutput(BaseModel):
    items: List[QuizItem]
