# AI Workflow Engine

Upload a document, describe what you want in plain English, and get back study material built from it — a summary, key concepts, and a quiz whose answers are checked against the source text.

The interesting part isn't the summary, it's the planner. You don't pick from a menu of features. You write something like *"summarize chapter one and give me five hard questions"*, a language model turns that into a small pipeline of steps, and the engine runs those steps in order, passing each one's output to the next. You can watch every step run in the UI.

## What it does

- **Document library.** Upload PDF, TXT or Markdown files (up to 25 MB, 10 per user). Text is extracted, split into overlapping ~600-token chunks, embedded, and stored in Postgres with pgvector. Processing runs in the background and the UI shows its progress.
- **Request → plan → run.** Your request is turned into an ordered list of steps. Each step reads one value from a shared state and writes another, e.g. `document → summary → quiz`.
- **Live progress.** Runs stream over server-sent events. The UI shows the planned pipeline up front, then each step's state, timing and token count as it executes.
- **Verified quizzes.** Generated questions go through a second pass where the model checks each answer against the source. Answers that fail are regenerated once; if they still fail they're kept but flagged, not silently dropped.
- **Semantic search** across your uploaded documents.
- **Practice and export.** Answer the quiz in the browser and track scores over time, or export a run as a PDF or an Anki deck.

### Available steps

| Step | Does |
|---|---|
| `summarize` | Condenses the text into a short paragraph of the main ideas |
| `extract_concepts` | Pulls out the five most important concepts |
| `generate_quiz` | Writes multiple-choice questions with explanations (easy / medium / hard) |
| `retrieve_chunks` | Embeds the request and fetches the most relevant chunks from the index |
| `verify_quiz` | Checks quiz answers against the source and regenerates failures |

Requests that don't map cleanly onto these steps fall back to a generic LLM step described by the planner, and requests that are too broad are rejected with a reason instead of producing something vague.

## Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy
- **LLM:** any model on Groq's OpenAI-compatible API (default `openai/gpt-oss-120b`), set with `LLM_MODEL`
- **Embeddings:** `all-MiniLM-L6-v2` via sentence-transformers, running locally — no API calls
- **Storage:** Postgres 16 + pgvector
- **Queue:** Redis + ARQ for background ingestion (optional; see `ASYNC_INGEST`)
- **Frontend:** React + Vite + Tailwind
- **Ops:** Docker, GitHub Actions, Prometheus metrics

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
| `LLM_MODEL` | `openai/gpt-oss-120b` | Model used for planning, steps and verification |
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
| `DELETE` | `/documents/{id}` | Delete a document and its chunks |
| `GET` | `/search?q=` | Semantic search across your documents |
| `POST` | `/run` | Run a workflow and return the result |
| `POST` | `/run/stream` | Same, streamed as server-sent events |
| `GET` | `/runs`, `/runs/{id}` | Run history |
| `GET` | `/runs/{id}/export/pdf`, `/runs/{id}/export/anki` | Export a run |
| `POST` | `/quiz/attempt` | Submit quiz answers for scoring |
| `GET` | `/quiz/history` | Past quiz scores |
| `GET` | `/health` | Checks the database and Redis; 503 if either is down |
| `GET` | `/metrics` | Prometheus metrics |

Interactive docs are at `/docs` when the server is running.

## Tests

```bash
ruff check engine/ api.py tests/
DATABASE_URL=postgresql://postgres:postgres@localhost:5433/ai_engine pytest tests/ -q
```

Integration tests run against a real Postgres and are skipped when `DATABASE_URL` isn't set. LLM calls and the embedding model are mocked, so the suite doesn't need an API key or network access.

## Deployment

The production image builds the React app and serves it from FastAPI, so the frontend and API share one origin and one container. On merge to `main`, GitHub Actions runs lint, tests and a frontend build, then smoke tests, waits for manual approval, and deploys over SSH: pull, rebuild the image, restart the container.

It currently runs on a single small VM, with Postgres on Supabase and Redis on Upstash. If you deploy to a machine with around 1 GB of RAM, add swap first — the image build won't survive without it.

## Known limitations

- **Long documents are truncated.** A run sends at most `MAX_DOC_CHARS` of the document to the model. Whole-book runs need retrieval or map-reduce summarization, which isn't built yet.
- **`retrieve_chunks` searches all of a user's documents**, not only the one selected for the run.
- **Runs are stateless.** There's no follow-up ("make question 3 harder"); each request starts fresh.
- **Auth is optional and currently off** in the deployed build.
- **One model for everything.** There's no fallback provider when the LLM is rate-limited.
