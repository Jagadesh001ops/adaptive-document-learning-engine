"""The user's request reaches every step: brief parsing, prompt context, quiz settings."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from engine.intent_parser import parse_intent
from engine.llm_nodes import (
    GenerateQuizNode,
    LLMNode,
    StepContext,
    SummarizeNode,
)
from engine.schemas import RequestBrief, WorkflowPlan

STEP = {"name": "summarize", "description": "Summarise", "input_key": "document", "output_key": "summary"}


def _plan(**extra):
    return {"goal": "g", "estimated_steps": 1, "steps": [STEP], **extra}


# ── Brief schema ──────────────────────────────────────────────────────────────

def test_plan_without_brief_gets_empty_brief():
    plan = WorkflowPlan(**_plan())
    assert plan.brief.preferences() == {}


def test_null_brief_is_treated_as_empty():
    assert WorkflowPlan(**_plan(brief=None)).brief.preferences() == {}


def test_brief_count_is_clamped_and_difficulty_normalised():
    assert RequestBrief(count=50).count == 20
    assert RequestBrief(count=0).count == 1
    assert RequestBrief(count="three").count is None
    assert RequestBrief(difficulty="Hard").difficulty == "hard"
    assert RequestBrief(difficulty="brutal").difficulty is None


def test_preferences_only_include_stated_fields():
    assert RequestBrief(format="bullet points", count=3).preferences() == {
        "format": "bullet points", "count": 3,
    }


# ── Planner ───────────────────────────────────────────────────────────────────

def _response(content):
    msg = SimpleNamespace(content=json.dumps(content))
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                           usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))


def test_parse_intent_reads_brief_and_tolerates_missing_one():
    fake = MagicMock()
    with patch("engine.intent_parser.get_client", return_value=fake):
        fake.chat.completions.create.return_value = _response(
            _plan(brief={"count": 3, "difficulty": "hard", "focus": None}))
        assert parse_intent("3 hard questions").brief.preferences() == {"count": 3, "difficulty": "hard"}

        fake.chat.completions.create.return_value = _response(_plan())
        assert parse_intent("summarise").brief.preferences() == {}


def test_planner_prompt_asks_for_brief():
    fake = MagicMock()
    fake.chat.completions.create.return_value = _response(_plan())
    with patch("engine.intent_parser.get_client", return_value=fake):
        parse_intent("anything")
    system = fake.chat.completions.create.call_args[1]["messages"][0]["content"]
    assert '"brief"' in system and "Never invent preferences" in system


# ── Context in step prompts ───────────────────────────────────────────────────

def _ctx(**brief):
    return StepContext(request="Summarise chapter 1 as bullet points and give me 3 hard questions",
                       goal="Study chapter 1",
                       instructions="Summarise chapter 1 as bullet points",
                       brief=RequestBrief(**brief))


def test_step_prompt_includes_request_goal_instructions_and_preferences():
    node = SummarizeNode(name="summarize", input_key="document", output_key="summary")
    node.set_context(_ctx(format="bullet points", focus="chapter 1"))
    system, user = node._build_messages({"document": "Some text."})

    assert "Summarise chapter 1 as bullet points and give me 3 hard questions" in system
    assert "Overall goal: Study chapter 1" in system
    assert "Your part: Summarise chapter 1 as bullet points" in system
    assert "- format: bullet points" in system and "- focus: chapter 1" in system
    assert "Do only your part" in system
    assert user == "Some text."


def test_no_context_leaves_prompt_unchanged():
    node = SummarizeNode(name="summarize", input_key="document", output_key="summary")
    system, _ = node._build_messages({"document": "x"})
    assert "request" not in system.lower() and "Do only your part" not in system


def test_generic_step_does_not_repeat_its_own_description():
    node = LLMNode(name="translate", description="Translate to French",
                   input_key="document", output_key="out")
    node.set_context(StepContext(request="translate it", instructions="Translate to French"))
    system, _ = node._build_messages({"document": "x"})
    assert "Your part:" not in system


# ── Quiz settings ─────────────────────────────────────────────────────────────

def _quiz_system(ctx=None, ui_difficulty=None):
    node = GenerateQuizNode(name="quiz", input_key="summary", output_key="quiz")
    if ctx:
        node.set_context(ctx)
    node._difficulty = ui_difficulty
    return node._build_messages({"summary": "Source text."})[0]


def test_quiz_defaults_to_five_medium_questions():
    system = _quiz_system()
    assert "exactly 5 multiple-choice questions at medium difficulty" in system


def test_quiz_uses_count_and_difficulty_from_request():
    system = _quiz_system(_ctx(count=3, difficulty="hard"))
    assert "exactly 3 multiple-choice questions at hard difficulty" in system


def test_request_difficulty_beats_ui_setting():
    assert "at easy difficulty" in _quiz_system(_ctx(difficulty="easy"), ui_difficulty="hard")
    assert "at hard difficulty" in _quiz_system(_ctx(), ui_difficulty="hard")


def test_quiz_is_told_to_stay_within_the_source():
    assert "do not rely on outside knowledge" in _quiz_system()
