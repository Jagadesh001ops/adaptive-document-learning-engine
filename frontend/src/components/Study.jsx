import { useEffect, useRef, useState } from "react"
import { apiFetch } from "../lib/api"
import { download } from "../lib/download"
import { readEvents } from "../lib/sse"
import { useNow } from "../lib/useNow"
import { Block } from "./Blocks"
import { DocumentStatus, StepRow } from "./RunProgress"

const ACCEPT = ".pdf,.txt,.md,text/plain,application/pdf,text/markdown"
const POLL_MS = 2000
const seconds = ms => `${(ms / 1000).toFixed(1)}s`

const STARTERS = [
  "What is this document about?",
  "Summarize it in 5 bullet points",
  "Explain the main argument, then give me 3 questions on it",
  "Quiz me on the whole document: 5 hard questions",
]

// ── choosing a document ──────────────────────────────────────────────────────

function DocPicker({ onPick }) {
  const [docs, setDocs] = useState(null)
  const [upload, setUpload] = useState(null)
  const inputRef = useRef(null)
  const unmounted = useRef(false)
  const now = useNow(upload?.status === "uploading" || upload?.status === "processing")

  useEffect(() => {
    unmounted.current = false
    apiFetch("/documents")
      .then(r => (r.ok ? r.json() : []))
      .then(all => setDocs(all.filter(d => d.status === "ready")))
      .catch(() => setDocs([]))
    return () => { unmounted.current = true }
  }, [])

  async function send(file) {
    if (!file) return
    setUpload({ name: file.name, status: "uploading", startedAt: Date.now() })
    try {
      const form = new FormData()
      form.append("file", file)
      const res = await apiFetch("/documents", { method: "POST", body: form })
      const body = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(body.detail || `Upload failed: ${res.status}`)
      for (;;) {
        if (unmounted.current) return
        const r = await apiFetch(`/documents/${body.document_id}`)
        if (!r.ok) throw new Error(`Could not check the document (${r.status})`)
        const doc = await r.json()
        if (doc.status === "ready") { onPick(doc); return }
        if (doc.status === "failed") throw new Error(doc.error_message || "Processing failed")
        setUpload(u => u && { ...u, status: "processing" })
        await new Promise(resolve => setTimeout(resolve, POLL_MS))
      }
    } catch (err) {
      setUpload(u => u && { ...u, status: "failed", error: err.message, finishedAt: Date.now() })
    }
  }

  const busy = upload && upload.status !== "failed"

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-semibold mb-1">Study</h2>
        <p className="text-gray-400 text-sm">
          Pick a document and study it in one conversation: ask questions, get summaries, be
          quizzed, change earlier answers, or combine them in a single message.
        </p>
      </div>

      <div
        onClick={() => !busy && inputRef.current?.click()}
        onDrop={e => { e.preventDefault(); if (!busy) send(e.dataTransfer.files?.[0]) }}
        onDragOver={e => e.preventDefault()}
        className={`border-2 border-dashed rounded-lg px-4 py-6 text-center transition ${
          busy ? "border-gray-700" : "border-gray-700 hover:border-gray-500 cursor-pointer"}`}
      >
        <input ref={inputRef} type="file" accept={ACCEPT} className="hidden"
          onChange={e => send(e.target.files?.[0])} />
        <p className="text-sm text-gray-400">
          Drop a new document here or <span className="text-white underline">browse</span>
        </p>
        <p className="text-xs text-gray-600">PDF, TXT, MD, up to 25 MB</p>
      </div>
      {upload && <DocumentStatus doc={upload} now={now} />}

      {docs === null ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : docs.length > 0 && (
        <div className="space-y-2">
          <p className="text-xs uppercase tracking-wide text-gray-500">Your documents</p>
          {docs.map(d => (
            <button
              key={d.document_id}
              onClick={() => onPick(d)}
              className="w-full text-left bg-gray-900 border border-gray-800 rounded-lg px-4 py-3 hover:border-gray-600 transition"
            >
              <p className="text-sm font-medium">{d.filename}</p>
              <p className="text-xs text-gray-500">{d.page_count} pages</p>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

// ── one turn ────────────────────────────────────────────────────────────────

function Steps({ steps, now, live }) {
  const [open, setOpen] = useState(false)
  if (!steps.length) return null
  const total = steps.reduce((t, s) => t + (s.time_ms || 0), 0)
  if (!live && !open) {
    return (
      <button onClick={() => setOpen(true)}
        className="text-xs font-mono text-gray-600 hover:text-gray-400 transition">
        {steps.length} steps · {seconds(total)}, show
      </button>
    )
  }
  return (
    <ol className="border border-gray-800 rounded-lg divide-y divide-gray-800 font-mono text-xs">
      {steps.map((s, i) => {
        const status = s.status || (s.success ? "done" : "failed")
        const detail = status === "running" ? seconds(now - s.startedAt)
          : status === "done"
            ? [s.detail, seconds(s.time_ms || 0), s.tokens ? `${s.tokens} tok` : null].filter(Boolean).join(" · ")
            : status
        return <StepRow key={i} index={i + 1} name={s.name} status={status} error={s.error} detail={detail} />
      })}
    </ol>
  )
}

function Turn({ turn, live, now, onAsk, busy, onExport, onQuizSubmit }) {
  return (
    <article id={turn.turn ? `turn-${turn.turn}` : undefined} className="space-y-4 scroll-mt-6">
      <p className="text-sm text-white bg-gray-900 border border-gray-800 rounded-lg px-4 py-2.5 whitespace-pre-wrap">
        {turn.message}
      </p>
      <Steps steps={turn.steps || []} now={now} live={live} />
      {live && !turn.steps?.length && <p className="text-xs font-mono text-amber-300">planning…</p>}
      {turn.reply && <p className="text-sm text-gray-200 whitespace-pre-wrap">{turn.reply}</p>}
      {(turn.blocks || []).map((b, i) => (
        <Block
          key={i}
          block={b}
          onAsk={onAsk}
          disabled={busy}
          onQuizSubmit={!live && turn.turn && onQuizSubmit ? (step, answers) => onQuizSubmit(turn.turn, step, answers) : null}
        />
      ))}
      {turn.error && <p className="text-sm text-red-400">{turn.error}</p>}
      {!live && turn.turn && turn.blocks?.length > 0 && (
        <p className="text-xs text-gray-600">
          Save this turn as{" "}
          <button onClick={() => onExport("pdf", turn.turn)} className="hover:text-gray-300">PDF</button>
          {" · "}
          <button onClick={() => onExport("docx", turn.turn)} className="hover:text-gray-300">Word</button>
        </p>
      )}
    </article>
  )
}

// ── the conversation ────────────────────────────────────────────────────────

// `start` comes from a suggested task in Library: a session to open, a message to send, or a turn
// to scroll to.
function Conversation({ doc, start, onChangeDoc }) {
  const [sessions, setSessions] = useState([])
  const [sessionId, setSessionIdState] = useState(null)
  // send() can run right after a session is opened, before React re-renders, so it reads the
  // current session from this ref rather than from state.
  const sessionRef = useRef(null)
  const startedRef = useRef(false)
  const setSessionId = id => { sessionRef.current = id; setSessionIdState(id) }
  const [turns, setTurns] = useState([])
  const [live, setLive] = useState(null)
  const [input, setInput] = useState("")
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const bottomRef = useRef(null)
  const busy = live !== null
  const now = useNow(busy)

  async function openSession(id) {
    setError(null)
    setSessionId(id)
    setTurns([])
    if (id === null) return
    const res = await apiFetch(`/sessions/${id}`)
    if (!res.ok) { setError("Couldn't load that session."); return [] }
    const loaded = (await res.json()).turns
    setTurns(loaded)
    return loaded
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    apiFetch(`/sessions?document_id=${doc.document_id}`)
      .then(r => (r.ok ? r.json() : []))
      .then(async list => {
        if (cancelled) return
        setSessions(list)
        const wanted = list.find(x => x.session_id === start?.session_id) ?? list[0]
        await openSession(wanted?.session_id ?? null)
        if (cancelled) return
        setLoading(false)
        if (startedRef.current) return
        startedRef.current = true
        if (start?.message) send(start.message)
        else if (start?.turn) {
          setTimeout(() => document.getElementById(`turn-${start.turn}`)?.scrollIntoView({ behavior: "smooth" }), 50)
        } else setTimeout(() => bottomRef.current?.scrollIntoView({ block: "end" }), 50)
      })
      .catch(() => { if (!cancelled) { setError("Couldn't load your sessions."); setLoading(false) } })
    return () => { cancelled = true }
    // Runs once per document; `start` is fixed for this Conversation (it is keyed by the task).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doc.document_id])

  // Follow the turn in progress as steps and outputs arrive.
  const liveKey = live ? `${live.steps.length}-${live.blocks.length}` : null
  useEffect(() => {
    if (liveKey) bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" })
  }, [liveKey])

  async function saveAnswers(turnNo, step, answers) {
    const res = await apiFetch(`/sessions/${sessionRef.current}/quiz-answers`, {
      method: "POST", body: JSON.stringify({ turn: turnNo, step, answers }),
    })
    if (!res.ok) {
      const body = await res.json().catch(() => ({}))
      throw new Error(body.detail || `${res.status}`)
    }
    // Keep the answers on the stored turn, so they survive switching sessions in this view.
    setTurns(ts => ts.map(t => t.turn !== turnNo ? t : {
      ...t, blocks: t.blocks.map(b => b.type === "quiz" && b.step === step ? { ...b, answers } : b),
    }))
  }

  async function ensureSession() {
    if (sessionRef.current !== null) return sessionRef.current
    const res = await apiFetch("/sessions", {
      method: "POST", body: JSON.stringify({ document_id: doc.document_id }),
    })
    const body = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(body.detail || `Couldn't start a session (${res.status})`)
    setSessionId(body.session_id)
    setSessions(s => [body, ...s])
    return body.session_id
  }

  async function send(text) {
    const message = (text ?? input).trim()
    if (!message || busy) return
    setInput("")
    setError(null)
    // The turn in progress lives here and is mirrored to state, so the end of the stream can
    // read it without depending on when React applies updates.
    let turn = { message, steps: [], blocks: [], reply: null, error: null }
    const update = fn => { turn = fn(turn); setLive(turn) }
    setLive(turn)
    let finished = null
    try {
      const id = await ensureSession()
      const res = await apiFetch(`/sessions/${id}/messages`, {
        method: "POST", body: JSON.stringify({ message }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || `Request failed: ${res.status}`)
      }
      await readEvents(res, event => {
        if (event.type === "step_start") {
          update(t => ({ ...t, steps: [...t.steps, { name: event.name, status: "running", startedAt: Date.now() }] }))
        } else if (event.type === "step") {
          // Step names repeat (two quizzes, two searches), so update the latest running one.
          update(t => {
            const i = t.steps.findLastIndex(s => s.name === event.name && s.status === "running")
            if (i < 0) return t
            const steps = [...t.steps]
            steps[i] = { ...event, status: event.success ? "done" : "failed" }
            return { ...t, steps }
          })
        } else if (event.type === "block") {
          update(t => ({ ...t, blocks: [...t.blocks, event.block] }))
        } else if (event.type === "reply") {
          update(t => ({ ...t, reply: event.text }))
        } else if (event.type === "error") {
          update(t => ({ ...t, error: event.message }))
        } else if (event.type === "done") {
          finished = event.turn
        }
      })
      if (finished) {
        setTurns(ts => [...ts, finished])
        setSessions(s => s.map(x => x.session_id === id
          ? { ...x, title: x.title || message.slice(0, 80), turns: (x.turns || 0) + 1 } : x))
      } else {
        // The stream ended without a stored turn: keep what arrived so the error stays visible.
        setTurns(ts => [...ts, { ...turn, error: turn.error || "The connection closed before the turn finished." }])
      }
    } catch (err) {
      setTurns(ts => [...ts, { ...turn, error: err.message }])
    } finally {
      setLive(null)
    }
  }

  async function exportAs(format, turnNo) {
    if (sessionRef.current === null) return
    try {
      const qs = new URLSearchParams({ format })
      if (turnNo) qs.set("turn", turnNo)
      await download(`/sessions/${sessionRef.current}/export?${qs}`, `study.${format}`)
    } catch (err) {
      setError(err.message)
    }
  }

  const hasContent = turns.some(t => t.blocks?.length)

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-2xl font-semibold mb-1 truncate">{doc.filename}</h2>
          <button onClick={onChangeDoc} disabled={busy}
            className="text-xs text-gray-500 hover:text-gray-300 transition disabled:opacity-40">
            ← another document
          </button>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={sessionId ?? ""}
            onChange={e => openSession(e.target.value === "" ? null : Number(e.target.value))}
            disabled={busy || loading}
            className="bg-gray-900 border border-gray-700 rounded-md px-2 py-1.5 text-xs text-gray-300 max-w-[14rem]"
          >
            <option value="">New session</option>
            {sessions.map(s => (
              <option key={s.session_id} value={s.session_id}>
                {(s.title || "Untitled").slice(0, 40)}{s.turns ? ` (${s.turns})` : ""}
              </option>
            ))}
          </select>
          <button onClick={() => exportAs("pdf")} disabled={!hasContent || busy}
            className="text-xs px-3 py-1.5 bg-gray-800 border border-gray-700 rounded-md text-gray-300 hover:text-white hover:border-gray-500 transition disabled:opacity-40">
            PDF
          </button>
          <button onClick={() => exportAs("docx")} disabled={!hasContent || busy}
            className="text-xs px-3 py-1.5 bg-gray-800 border border-gray-700 rounded-md text-gray-300 hover:text-white hover:border-gray-500 transition disabled:opacity-40">
            Word
          </button>
        </div>
      </div>

      {loading ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : turns.length === 0 && !live && (
        <div className="space-y-3">
          <p className="text-sm text-gray-500">
            Ask anything, or ask for several things at once. Later you can say things like
            "make question 2 harder" or "shorter, please" to change what you got.
          </p>
          <div className="flex flex-wrap gap-2">
            {STARTERS.map(s => (
              <button key={s} onClick={() => send(s)}
                className="text-xs px-3 py-1.5 rounded-full border border-gray-700 text-gray-300 hover:border-gray-500 hover:text-white transition">
                {s}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="space-y-12">
        {turns.map((t, i) => (
          <Turn key={t.turn ?? `x${i}`} turn={t} now={now} onAsk={send} busy={busy} onExport={exportAs}
            onQuizSubmit={saveAnswers} />
        ))}
        {live && <Turn turn={live} live now={now} onAsk={send} busy />}
      </div>

      {error && <p className="text-sm text-red-400">{error}</p>}

      <div ref={bottomRef} className="flex gap-2 items-end sticky bottom-0 bg-gray-950 border-t border-gray-900 py-4 scroll-mt-24">
        <textarea
          value={input}
          rows={input.includes("\n") ? 3 : 1}
          maxLength={2000}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send() } }}
          disabled={busy || loading}
          placeholder={turns.length ? "Ask, or change something above…" : "e.g. Why did the Church fear print? Then give me 2 questions on it."}
          className="flex-1 resize-none bg-gray-900 border border-gray-700 rounded-lg px-4 py-3 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-gray-500 disabled:opacity-50"
        />
        <button
          onClick={() => send()}
          disabled={busy || loading || !input.trim()}
          className="px-5 py-3 bg-white text-gray-950 font-medium text-sm rounded-lg hover:bg-gray-100 transition disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {busy ? "Working…" : "Send"}
        </button>
      </div>
    </div>
  )
}

// task: {doc, message?, session_id?, turn?, id} — `id` changes for each new task so the
// conversation restarts and runs it.
export default function Study({ task, onTask }) {
  if (!task?.doc) return <DocPicker onPick={doc => onTask({ doc, id: Date.now() })} />
  return (
    <Conversation key={`${task.doc.document_id}-${task.id}`} doc={task.doc} start={task}
      onChangeDoc={() => onTask(null)} />
  )
}
