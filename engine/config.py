import os

# Groq free-tier limits are per model, so separate jobs use separate models.
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")          # workflow steps
ANSWER_MODEL = os.getenv("ANSWER_MODEL", LLM_MODEL)                # document Q&A answers/revisions
CHECK_MODEL = os.getenv("CHECK_MODEL", "qwen/qwen3.8-27b")         # independent judge
PLANNER_MODEL = os.getenv("PLANNER_MODEL", "openai/gpt-oss-20b")   # request -> plan
