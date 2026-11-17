from dotenv import load_dotenv

load_dotenv()

import json
from typing import List, Optional

from .config import PLANNER_MODEL
from .schemas import WorkflowPlan

client = None


def get_client():
    global client
    if client is None:
        from groq import Groq
        client = Groq()
    return client


def _build_system_prompt(available_nodes: List[dict]) -> str:
    node_list = "\n".join(
        f'  - "{n["name"]}": {n["description"]}' for n in available_nodes
    )
    return f"""You are an AI workflow planner for a document study-kit system.

Available step types (use ONLY these names or their obvious variants):
{node_list}

Analyse the user's request and return ONLY a JSON object:
{{
    "goal": "user's intent in one sentence",
    "estimated_steps": 3,
    "scope": "single_document",
    "too_broad": false,
    "too_broad_reason": null,
    "brief": {{
        "format": null,
        "length": null,
        "count": null,
        "difficulty": null,
        "focus": null,
        "audience": null,
        "notes": null
    }},
    "steps": [
        {{
            "name": "snake_case_step_name",
            "description": "Instruction for this step, specific to the user's request",
            "input_key": "key this step reads from",
            "output_key": "key this step writes to"
        }}
    ]
}}

Brief rules:
- Record only preferences the user actually stated; leave everything else null. Never invent preferences.
- format: how output should look ("bullet points", "table"). length: how long ("two sentences", "detailed").
- count: number of items requested (e.g. "three questions" -> 3). difficulty: "easy", "medium" or "hard".
- focus: the part or topic of the document to concentrate on. audience: who it is for ("a 10-year-old").
- notes: any other explicit instruction that doesn't fit the fields above.

Step description rules:
- Write each description as a direct instruction to that step, carrying over what the user asked for
  that is relevant to it (focus, format, count, difficulty). Example: for "explain the causes of the
  French Revolution simply and give me 3 hard questions", the summarize step's description is
  "Summarize the causes of the French Revolution in plain language" and the quiz step's is
  "Write 3 hard questions on the causes of the French Revolution".

Scope rules:
- "single_document"  — request concerns one specific document
- "multi_document"   — request explicitly references several documents
- "library"          — request asks about all documents / the whole library

Too-broad rules:
- If the request is so vague that no reliable plan can be made, set too_broad=true,
  write a helpful too_broad_reason, and return steps=[one placeholder step].

Step rules:
- Each step must be atomic — one job only
- Use snake_case for all step names — match the available step types exactly
- First step always reads from "document" (or "retrieved_chunks" if a retrieval step ran first)
- Each step's input_key must match the previous step's output_key
- estimated_steps must equal the actual number of steps in the array"""


def parse_intent(
    user_request: str,
    available_nodes: Optional[List[dict]] = None,
) -> WorkflowPlan:
    """Parse *user_request* into a WorkflowPlan.

    Args:
        user_request:    Natural-language request from the user.
        available_nodes: List of {name, description} dicts from the registry.
                         If omitted, fetched automatically from the default registry.
    """
    if available_nodes is None:
        from .registry import get_registry
        available_nodes = get_registry().describe()

    response = get_client().chat.completions.create(
        model=PLANNER_MODEL,
        messages=[
            {"role": "system", "content": _build_system_prompt(available_nodes)},
            {"role": "user",   "content": user_request},
        ],
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content
    data = json.loads(raw)

    # Back-compat: older responses won't have scope/too_broad
    data.setdefault("scope", "single_document")
    data.setdefault("too_broad", False)
    data.setdefault("too_broad_reason", None)
    data.setdefault("brief", {})

    return WorkflowPlan(**data)
