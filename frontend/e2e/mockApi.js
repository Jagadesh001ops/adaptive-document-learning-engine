// A small in-memory stand-in for the API, enough for the Library and Study flows.
// API calls are matched by path on any host, since the dev build's API address depends on
// VITE_API_URL (unset in CI, which means http://localhost:8000).
const API_PATH = /^\/(documents|sessions|runs|search|quiz|health)(\/|$)/

export const DOC = {
  document_id: 3, filename: "print-culture.pdf", content_type: "application/pdf",
  size_bytes: 5_466_000, page_count: 26, char_count: 52_000, status: "ready", error_message: null,
}

const SECTIONS = [
  ["Print Arrives", "new"], ["Gutenberg's Press", "new"], ["Fear of Print", "new"],
  ["The Reading Mania", "new"],
].map(([title, status], i) => ({
  section: i + 1, title, pages: [i * 6 + 1, i * 6 + 6], status, coverage: 0, right: 0, wrong: 0,
}))

const QUIZ = [
  { question: "Who built the first movable-type press in Europe?",
    options: { A: "Gutenberg", B: "Luther", C: "Erasmus", D: "Caxton" }, answer: "A",
    explanation: "Gutenberg, around 1440.", verified: true,
    citation: { chunk_id: 4, excerpt: "Gutenberg developed the first-known printing press" } },
  { question: "What did the Church start in 1558?",
    options: { A: "A printing guild", B: "An Index of Prohibited Books", C: "Free schools", D: "A library" },
    answer: "B", explanation: "To control what was read.", verified: true,
    citation: { chunk_id: 9, excerpt: "began to maintain an Index of Prohibited Books from 1558" } },
]

function sse(events) {
  return events.map(e => `data: ${JSON.stringify(e)}\n\n`).join("")
}

export async function mockApi(page, { withOpenQuiz = false } = {}) {
  const state = {
    turns: [],
    sessions: [],
    sentMessages: [],
    quizAnswers: [],
  }
  if (withOpenQuiz) {
    state.sessions = [{ session_id: 1, document_id: 3, title: "quiz me", turns: 1 }]
    state.turns = [{ turn: 1, message: "Give me 2 questions on section 3", steps: [],
                     blocks: [{ type: "quiz", step: 0, items: QUIZ, section: 3, difficulty: "medium" }] }]
  }

  const progress = () => {
    const answered = state.quizAnswers.length > 0
    const open = state.turns.some(t => t.blocks.some(b => b.type === "quiz")) && !answered
    const sections = SECTIONS.map(s => s.section === 3 && answered
      ? { ...s, status: "tested", right: 1, wrong: 1, coverage: 0.5 } : s)
    let next = { kind: "overview", label: "Get an overview",
                 detail: "Start with the big picture before going section by section.",
                 message: "Give me an overview of the whole document in 5 bullet points" }
    if (open) next = { kind: "finish_quiz", label: "Finish your quiz",
                       detail: "2 questions from turn 1 not answered yet.", message: null,
                       session_id: 1, turn: 1 }
    if (answered) next = { kind: "revisit", label: "Go back over Fear of Print",
                           detail: "You missed 1 question on section 3.", section: 3,
                           message: "Explain the main points of section 3 (Fear of Print) again, then give me 3 questions on it" }
    return { document_id: 3, sections, next, latest_session_id: state.sessions[0]?.session_id ?? null,
             done: { sessions: state.sessions.length, questions_asked: 0, summaries: 0, concepts: 0,
                     quizzes: state.turns.length ? 1 : 0, answered: state.quizAnswers.length ? 2 : 0,
                     correct: state.quizAnswers.length ? 1 : 0 } }
  }

  await page.route(url => API_PATH.test(url.pathname), async route => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname
    const json = (body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) })

    if (path === "/documents" && req.method() === "GET") return json([DOC])
    if (path === "/documents/3/progress") return json(progress())
    if (path === "/sessions" && req.method() === "GET") return json(state.sessions)
    if (path === "/sessions" && req.method() === "POST") {
      const sess = { session_id: 1, document_id: 3, title: null, turns: 0 }
      state.sessions = [sess]
      return json(sess, 201)
    }
    if (path === "/sessions/1" && req.method() === "GET") {
      return json({ session_id: 1, document_id: 3, document: DOC, turns: state.turns })
    }
    if (path === "/sessions/1/messages") {
      const { message } = req.postDataJSON()
      state.sentMessages.push(message)
      const blocks = [
        { type: "summary", step: 0, scope: "whole", text: "- **Print** spread ideas\n- Literacy grew" },
        { type: "quiz", step: 1, items: QUIZ, difficulty: "medium" },
      ]
      const turn = { turn: state.turns.length + 1, message, blocks, error: null,
                     steps: [{ name: "plan", success: true, time_ms: 900, tokens: 800, detail: "summarize -> quiz" },
                             { name: "summary", success: true, time_ms: 1200, tokens: 2000, detail: "40 words" },
                             { name: "quiz", success: true, time_ms: 1500, tokens: 2500, detail: "2 questions" }] }
      state.turns.push(turn)
      return route.fulfill({
        status: 200, contentType: "text/event-stream",
        body: sse([
          { type: "step_start", name: "plan" }, { type: "step", ...turn.steps[0] },
          { type: "plan", goal: "overview", steps: [] },
          { type: "step_start", name: "summary" }, { type: "step", ...turn.steps[1] },
          { type: "block", block: blocks[0] },
          { type: "step_start", name: "quiz" }, { type: "step", ...turn.steps[2] },
          { type: "block", block: blocks[1] },
          { type: "done", turn },
        ]),
      })
    }
    if (path === "/sessions/1/quiz-answers") {
      const body = req.postDataJSON()
      state.quizAnswers.push(body)
      const turn = state.turns.find(t => t.turn === body.turn)
      const block = turn.blocks.find(b => b.type === "quiz" && b.step === body.step)
      const results = Object.entries(body.answers).map(([n, chosen]) => ({
        question: Number(n), chosen, correct: block.items[n - 1].answer,
        is_correct: chosen === block.items[n - 1].answer }))
      return json({ correct: results.filter(r => r.is_correct).length, total: results.length, results })
    }
    return json({ detail: `not mocked: ${req.method()} ${path}` }, 404)
  })
  return state
}
