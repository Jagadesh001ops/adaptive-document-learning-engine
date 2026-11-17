# Adaptive Document Learning Engine

Upload a document and study it in a conversation. Ask why something happened, ask for a summary, ask to be quizzed, ask to make question 2 harder, or do several of those in one message. Every answer is grounded in the document and checked against it.

The interesting part isn't the summary, it's the planner. You don't pick from a menu of features. You write something like *"summarize chapter one and give me five hard questions"*, a language model turns that into a small pipeline of steps, and the engine runs those steps in order, passing each one's output to the next. You can watch every step run in the UI.

## What it does

- **Study sessions.** One conversation per document, and any message can ask for anything. A planner reads the message and the conversation so far and composes a plan from five tools: `answer`, `summarize`, `concepts`, `quiz` and `revise`, in any order and number. *"Explain why the Church feared print, then give me 3 hard questions on it"* runs an answer and then a quiz built on that answer. *"Make question 2 easier"* rewrites only question 2 of the earlier quiz and leaves the others exactly as they were. Each step has its own settings, so "4 bullet points and 3 questions" gets 4 bullets and 3 questions, never 4 of each.
- **Memory.** Follow-ups like *"and why did that matter?"* work: the planner turns each message into a standalone search using the conversation, and a context keeper remembers which passages the conversation relied on and reads them again with fresh search results.
- **Quizzes grounded in the text.** Questions are written from document passages, not from a summary. Each one cites a passage, and code checks that the quoted excerpt really appears there; questions that fail are flagged, not hidden. The requested number of questions is enforced in code.
- **Word-style exports.** Download a whole session, or one turn of it, as a PDF or a Word document. Each request becomes a heading, quotes are set as block quotes, questions are numbered with indented options, and the answer key is on its own page.
- **Document library.** Upload PDF, TXT or Markdown files (up to 25 MB, 10 per user). Text is extracted, split into overlapping ~600-token chunks, embedded, and stored in Postgres with pgvector. Processing runs in the background and the UI shows its progress.
- **Progress and what to do next.** Each document is mapped into up to 8 sections, titled once by a small model. The Library shows which sections have been read, quizzed and answered well or badly, what you've done so far, and one suggested next task: get an overview, finish an open quiz, go back over a section you missed questions on, study the next section, test yourself on one you've only read, and finally test the whole document. The suggestion is picked by rules in code and opens the study session with that request. You can also name sections yourself: "summarize section 3" or "quiz me on pages 9-12" reads exactly that part.
- **Request → plan → run.** Your request is turned into an ordered list of steps. Each step reads one value from a shared state and writes another, e.g. `document → summary → quiz`.
- **The request shapes every step.** The planner also pulls out what you asked for — format, length, number of questions, difficulty, focus, audience — and each step sees your original request, the overall goal and its own instructions. "Summarize chapter one as bullet points and give me three hard questions" gets bullet points and three hard questions, not a stock paragraph and five medium ones.
- **Live progress.** Runs stream over server-sent events. The UI shows the planned pipeline up front, then each step's state, timing and token count as it executes.
- **Quiz verification.** When a plan includes the `verify_quiz` step, the model checks each answer against the retrieved source passages. Answers that fail are regenerated once; if they still fail they're kept but flagged, not silently dropped.
- **Ask the document anything.** Facts, why something happened, comparisons, what an idea means. Each answer comes with a justification and exact quotes from the passages it used. Before you see it, the quotes are checked against the text and a second model judges whether they really support the answer. If not, the system re-reads the document for what's missing — new passages only — and revises, up to twice. If it still can't support an answer it says so instead of guessing. Every answer ends with follow-up questions you can click.
- **Claude as supervisor.** Every conversation turn, Ask and run is logged in full, failures included. Running `/review-logs` in Claude Code fetches the unreviewed logs, and Claude reviews each one the way a teacher marks work: was it correct, grounded in the source, what the user asked for, and did the checker judge it right. Problems are tagged with the role responsible, and Claude writes general lessons for each role, then saves the reviews back to the app. `GET /admin/reviews/summary` aggregates the results per model.
- **Semantic search** across your uploaded documents.
- **One screen.** The Study tab is the whole workflow: pick or upload a document, then talk to it. Answers, summaries, concepts and quizzes appear in the conversation, questions can be answered and checked in place, earlier sessions for the same document can be reopened, and the session or a single turn can be saved as PDF or Word.

### Available steps

| Step | Does |
|---|---|
| `summarize` | Condenses the text to its main ideas (a short paragraph unless you ask otherwise) |
| `extract_concepts` | Pulls out the key concepts (five unless you ask for a different number) |
| `generate_quiz` | Writes multiple-choice questions with explanations, grounded in the source (five at medium difficulty unless you ask otherwise) |
| `retrieve_chunks` | Embeds the request and fetches the most relevant chunks from the index |
| `verify_quiz` | Checks quiz answers against the source and regenerates failures |

Requests that don't map cleanly onto these steps fall back to a generic LLM step described by the planner, and requests that are too broad are rejected with a reason instead of producing something vague.

## Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy
- **LLM:** models on Groq. Different jobs use different models — free-tier limits are per model, and an answer is better checked by a model other than the one that wrote it (see Configuration)
- **Embeddings:** `all-MiniLM-L6-v2` via sentence-transformers, running locally — no API calls
- **Storage:** Postgres 16 + pgvector
- **Queue:** Redis + ARQ for background ingestion (optional; see `ASYNC_INGEST`)
- **Frontend:** React + Vite + Tailwind
- **Ops:** Docker, GitHub Actions, Prometheus metrics

## How a study turn works

```
message + conversation ──► planner ──► [answer] ──► [quiz, building on the answer] ──► blocks
                               │            │                │
                     standalone search   find → answer → check → re-read → revise
                                         passages from search + context keeper
```

1. The planner (`PLANNER_MODEL`) returns a goal, a standalone search query and a list of tool steps, each with its own count, difficulty, format and scope (whole document or one topic). Greetings and unclear messages get a short reply instead.
2. Each step runs in order and produces a block (an answer, a summary, concepts or a quiz) that streams to the browser as soon as it's ready. A step can build on the previous step's output.
3. `revise` finds the earlier output the user means (a turn, and a question number if named), sends only that part to the model with its source passages, and keeps everything else untouched.
4. The turn is saved to the conversation, the context keeper's memory is updated, and the full record (history, plan, steps, blocks, passages) is logged for review.

## How a run works

```
request ──► planner (LLM) ──► [step 1] ──► [step 2] ──► ... ──► result
                                  │            │
                          shared state: document, query, summary, quiz, ...
```

1. If the run references a library document, the backend loads that document's stored text (chunk overlap removed, capped at `MAX_DOC_CHARS`).
2. The planner returns a goal and a list of steps, each with an input key and an output key.
3. The engine runs the steps in order. Each step reads its input from the shared state and writes its output back.
4. Progress events stream to the browser, and the finished run is saved so it can be reopened, practiced or exported.

## Running locally

You'll need Docker, Python 3.12, Node 20 and a [Groq API key](https://console.groq.com/keys).

**1. Configure.** Create a `.env` in the repo root:

```bash
GROQ_API_KEY=your-key
LLM_MODEL=openai/gpt-oss-120b
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_DB=ai_engine
```

**2. Start everything with Docker Compose:**

```bash
docker compose up --build
```

This starts the app, the ingestion worker, Postgres with pgvector and Redis. Open http://localhost:8000 — the image builds the React app and FastAPI serves it alongside the API.

**Or run the pieces yourself** for faster iteration:

```bash
docker compose up db redis -d

python -m venv venv && source venv/bin/activate   # venv\Scripts\activate on Windows
pip install -r requirements.txt
DATABASE_URL=postgresql://postgres:postgres@localhost:5433/ai_engine uvicorn api:app --reload

cd frontend && npm ci && npm run dev               # http://localhost:5173
```

The first upload downloads the embedding model (~90 MB) if it isn't cached yet.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | — | Required for any LLM call |
| `LLM_MODEL` | `openai/gpt-oss-120b` | Workflow steps (summaries, quizzes, ...) |
| `ANSWER_MODEL` | same as `LLM_MODEL` | Answers and revisions in Ask |
| `CHECK_MODEL` | `qwen/qwen3.8-27b` | Independent check of Ask answers |
| `PLANNER_MODEL` | `openai/gpt-oss-20b` | Turns a request into a plan |
| `ADMIN_TOKEN` | — | Protects the `/admin/*` review routes; they don't exist until it's set |
| `DATABASE_URL` | — | Postgres connection string (pgvector required) |
| `REDIS_URL` | `redis://localhost:6379` | Queue for background ingestion; `rediss://` for TLS |
| `ASYNC_INGEST` | `1` | `0` processes uploads in the API process instead of the ARQ worker — useful on a single small server |
| `MAX_DOC_CHARS` | `24000` | How much of a document a run sends to the model |
| `SUPABASE_JWT_SECRET` | — | Enables JWT auth. When unset, every request runs as a single dev user |
| `CORS_ORIGINS` | — | Extra allowed origins, comma-separated |
| `MAINTENANCE_MODE` | `0` | `1` returns 503 on everything except `/health` and `/metrics` |
| `DB_SSL` | — | `1` forces `sslmode=require` (automatic for Supabase URLs) |

## API

| Method | Path | |
|---|---|---|
| `POST` | `/documents` | Upload a file; returns immediately with `status: processing` |
| `GET` | `/documents`, `/documents/{id}` | List documents / check processing status |
| `GET` | `/documents/{id}/progress` | Sections, what has been studied and tested, and the suggested next task |
| `DELETE` | `/documents/{id}` | Delete a document and its chunks |
| `POST` | `/sessions` | Start a study session on a document |
| `GET` | `/sessions?document_id=`, `/sessions/{id}` | List sessions / a session with all its turns |
| `POST` | `/sessions/{id}/messages` | Send a message; the turn's plan, steps and outputs stream as server-sent events |
| `GET` | `/sessions/{id}/export?format=pdf\|docx&turn=&page_size=A4\|letter` | Download the session, or one turn, as a Word-style PDF or DOCX |
| `POST` | `/sessions/{id}/quiz-answers` | Grade and store answers to one quiz in the session (`{turn, step, answers}`) |
| `DELETE` | `/sessions/{id}` | Delete a session |
| `POST` | `/documents/{id}/ask` | Ask a single question about one document, streamed as server-sent events |
| `GET` | `/search?q=` | Semantic search across your documents |
| `POST` | `/run` | Run a workflow and return the result |
| `POST` | `/run/stream` | Same, streamed as server-sent events |
| `GET` | `/runs`, `/runs/{id}` | Run history |
| `GET` | `/runs/{id}/export/pdf`, `/export/docx`, `/export/anki` | Export a run |
| `POST` | `/quiz/attempt` | Submit quiz answers for scoring |
| `GET` | `/quiz/history` | Past quiz scores |
| `GET` | `/health` | Checks the database and Redis; 503 if either is down |
| `GET` | `/admin/logs` | Unreviewed interaction logs with full records (needs `X-Admin-Token`) |
| `POST` | `/admin/logs/{id}/review` | Save a supervisor review (needs `X-Admin-Token`) |
| `GET` | `/admin/reviews/summary` | Review results per model, problems by role, latest lessons (needs `X-Admin-Token`) |
| `GET` | `/metrics` | Prometheus metrics |

Interactive docs are at `/docs` when the server is running.

## Reviewing logs with Claude

Set `ADMIN_TOKEN` on the server, then locally:

```bash
export ALE_ADMIN_TOKEN=<same token>
claude          # in this repo
/review-logs    # or /review-logs 50 to review more at once
```

Claude fetches pending logs, reviews each against the source it was based on, posts the reviews to `/admin/logs/{id}/review`, and reports the main problems and lessons per role. The rubric lives in `.claude/commands/review-logs.md`.

## Tests

```bash
ruff check engine/ api.py tests/
DATABASE_URL=postgresql://postgres:postgres@localhost:5433/ai_engine pytest tests/ -q
```

Integration tests run against a real Postgres and are skipped when `DATABASE_URL` isn't set. LLM calls and the embedding model are mocked, so the suite doesn't need an API key or network access.

## Deployment

The production image builds the React app and serves it from FastAPI, so the frontend and API share one origin and one container. On merge to `main`, GitHub Actions runs lint, tests and a frontend build, then smoke tests, waits for manual approval, and deploys over SSH: pull, rebuild the image, restart the container.

It currently runs on a single small VM behind [Caddy](https://caddyserver.com/), which handles HTTPS (Let's Encrypt) and forwards to the app on `localhost:8000`; the app port itself isn't exposed publicly. Postgres is on Supabase and Redis on Upstash. If you deploy to a machine with around 1 GB of RAM, add swap first — the image build won't survive without it.

## Known limitations

- **Long documents are truncated.** A run sends at most `MAX_DOC_CHARS` of the document to the model. Whole-book runs need retrieval or map-reduce summarization, which isn't built yet.
- **Quiz verification is optional.** It only runs when the planner includes `verify_quiz`, it needs a retrieval step before it, and the same model grades its own answers.
- **Old runs stay viewable, not editable.** The browser now has one Study screen per document. Runs made before it are still listed in History, and the `/run` and `/documents/{id}/ask` endpoints remain for API use, but they have no memory between requests.
- **Section pages are approximate.** Chunks don't record page numbers, so a section's pages are estimated from its position in the text.
- **Revised answers aren't re-checked.** When `revise` rewrites an answer it's marked unchecked; revised quiz questions get the quote check only.
- **Auth is optional and currently off** in the deployed build.
- **No fallback provider.** When a Groq model is rate-limited, the request fails with a "try again" message.
- **Reviews read real user content.** `/review-logs` pulls logged questions, answers and document passages into your Claude Code session. Fine for study material; think twice once real users upload private documents.
- **Claude's advice isn't applied yet.** Lessons are collected and shown in the summary, but not yet fed back into the other models' prompts.
