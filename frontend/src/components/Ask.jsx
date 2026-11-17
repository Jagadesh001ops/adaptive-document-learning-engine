import { useEffect, useRef, useState } from "react"
import { apiFetch } from "../lib/api"
import { readEvents } from "../lib/sse"
import { useNow } from "../lib/useNow"
import { StepRow } from "./RunProgress"

const seconds = ms => `${(ms / 1000).toFixed(1)}s`

const VERIFICATION = {
  checked: { text: "Checked against the passages", tone: "text-emerald-400" },
  revised: { text: "Corrected after re-reading", tone: "text-emerald-400" },
  unverified: { text: "Couldn't fully verify this answer — treat it with care", tone: "text-amber-300" },
  unchecked: { text: "Not checked — the checker was unavailable", tone: "text-amber-300" },
}

function useReadyDocs() {
  const [docs, setDocs] = useState([])
  useEffect(() => {
    apiFetch("/documents")
      .then(r => (r.ok ? r.json() : []))
      .then(all => setDocs(all.filter(d => d.status === "ready")))
      .catch(() => {})
  }, [])
  return docs
}

function Steps({ steps, now }) {
  return (
    <ol className="border border-gray-800 rounded-lg divide-y divide-gray-800 font-mono text-xs">
      {steps.map((s, i) => (
        <StepRow
          key={s.name}
          index={i + 1}
          name={s.name}
          status={s.status}
          error={s.error}
          detail={
            s.status === "running" ? seconds(now - s.startedAt)
              : s.status === "done" ? [s.detail, seconds(s.time_ms), s.tokens ? `${s.tokens} tok` : null].filter(Boolean).join(" · ")
              : s.status
          }
        />
      ))}
    </ol>
  )
}

function Answer({ result, onAsk, disabled }) {
  const v = VERIFICATION[result.verification] ?? VERIFICATION.unchecked
  return (
    <div className="space-y-4">
      {result.status === "not_in_document" && (
        <p className="text-xs uppercase tracking-wide text-gray-500">The document doesn't cover this directly</p>
      )}
      <p className="text-gray-100 leading-relaxed whitespace-pre-wrap">{result.answer}</p>

      {result.justification && (
        <div>
          <p className="text-xs uppercase tracking-wide text-gray-500 mb-1">Why</p>
          <p className="text-sm text-gray-400 leading-relaxed whitespace-pre-wrap">{result.justification}</p>
        </div>
      )}

      {result.citations?.length > 0 && (
        <div className="space-y-2">
          <p className="text-xs uppercase tracking-wide text-gray-500">From the document</p>
          {result.citations.map((c, i) => (
            <blockquote key={i} className="border-l-2 border-gray-700 pl-3 text-sm text-gray-300">
              “{c.quote}”
              <span className="block text-xs text-gray-600 font-mono mt-0.5">passage {c.chunk_id}</span>
            </blockquote>
          ))}
        </div>
      )}

      {result.general_knowledge && (
        <div className="rounded-lg border border-gray-800 px-3 py-2">
          <p className="text-xs uppercase tracking-wide text-gray-500 mb-1">Beyond the book</p>
          <p className="text-sm text-gray-400 whitespace-pre-wrap">{result.general_knowledge}</p>
        </div>
      )}

      <p className={`text-xs ${v.tone}`}>
        {v.text}
        {result.verification === "revised" && ` (${result.revisions}×)`}
        <span className="text-gray-600"> · {result.passages_read} passages read</span>
      </p>
      {result.problems?.length > 0 && (
        <ul className="text-xs text-amber-300/80 list-disc pl-5 space-y-0.5">
          {result.problems.map((p, i) => <li key={i}>{p}</li>)}
        </ul>
      )}

      {result.follow_ups?.length > 0 && (
        <div className="flex flex-wrap gap-2 pt-1">
          {result.follow_ups.map(q => (
            <button
              key={q}
              disabled={disabled}
              onClick={() => onAsk(q)}
              className="text-xs px-3 py-1.5 rounded-full border border-gray-700 text-gray-300 hover:border-gray-500 hover:text-white transition disabled:opacity-40"
            >
              {q}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

export default function Ask({ doc, onPickDoc }) {
  const docs = useReadyDocs()
  const [question, setQuestion] = useState("")
  const [exchanges, setExchanges] = useState([])
  const [busy, setBusy] = useState(false)
  const bottomRef = useRef(null)
  const now = useNow(busy)

  useEffect(() => { setExchanges([]) }, [doc?.document_id])
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" }) }, [exchanges.length])

  const updateLast = fn => setExchanges(xs => xs.map((x, i) => (i === xs.length - 1 ? fn(x) : x)))

  async function ask(q) {
    const text = (q ?? question).trim()
    if (!text || busy || !doc) return
    setQuestion("")
    setBusy(true)
    setExchanges(xs => [...xs, { question: text, steps: [], result: null, error: null, showSteps: false }])
    try {
      const res = await apiFetch(`/documents/${doc.document_id}/ask`, {
        method: "POST",
        body: JSON.stringify({ question: text }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || `Request failed: ${res.status}`)
      }
      await readEvents(res, event => {
        if (event.type === "step_start") {
          updateLast(x => ({ ...x, steps: [...x.steps, { name: event.name, status: "running", startedAt: Date.now() }] }))
        } else if (event.type === "step") {
          updateLast(x => ({
            ...x,
            steps: x.steps.map(s => s.name === event.name
              ? { ...s, status: event.success ? "done" : "failed", time_ms: event.time_ms,
                  tokens: event.tokens, detail: event.detail, error: event.error }
              : s),
          }))
        } else if (event.type === "done") {
          updateLast(x => ({ ...x, result: event }))
        } else if (event.type === "error") {
          updateLast(x => ({ ...x, error: event.message }))
        }
      })
    } catch (err) {
      updateLast(x => ({ ...x, error: err.message }))
    } finally {
      setBusy(false)
    }
  }

  if (!doc) {
    return (
      <div className="space-y-4">
        <div>
          <h2 className="text-2xl font-semibold mb-1">Ask</h2>
          <p className="text-gray-400 text-sm">Pick a document, then ask anything about it.</p>
        </div>
        {docs.length === 0 ? (
          <p className="text-gray-500 text-sm">No ready documents yet. Upload one in Library first.</p>
        ) : (
          <div className="space-y-2">
            {docs.map(d => (
              <button
                key={d.document_id}
                onClick={() => onPickDoc(d)}
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

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between gap-4">
        <div className="min-w-0">
          <h2 className="text-2xl font-semibold mb-1">Ask</h2>
          <p className="text-gray-400 text-sm truncate">{doc.filename}</p>
        </div>
        <select
          value={doc.document_id}
          onChange={e => onPickDoc(docs.find(d => d.document_id === Number(e.target.value)))}
          disabled={busy}
          className="bg-gray-900 border border-gray-700 rounded-lg px-2 py-1.5 text-xs text-gray-300 max-w-[50%]"
        >
          {docs.map(d => <option key={d.document_id} value={d.document_id}>{d.filename}</option>)}
        </select>
      </div>

      {exchanges.length === 0 && (
        <p className="text-sm text-gray-500">
          Ask anything — facts, why something happened, comparisons, what an idea means. Answers
          quote the passages they rely on and are checked before you see them.
        </p>
      )}

      <div className="space-y-10">
        {exchanges.map((x, i) => {
          const done = x.result || x.error
          const totalMs = x.steps.reduce((t, s) => t + (s.time_ms || 0), 0)
          return (
            <div key={i} className="space-y-3">
              <p className="text-sm font-medium text-white">{x.question}</p>
              {!done || x.showSteps ? (
                <Steps steps={x.steps} now={now} />
              ) : (
                <button
                  onClick={() => setExchanges(xs => xs.map((e, j) => (j === i ? { ...e, showSteps: true } : e)))}
                  className="text-xs font-mono text-gray-600 hover:text-gray-400 transition"
                >
                  {x.steps.length} steps · {seconds(totalMs)} — show
                </button>
              )}
              {x.error && <p className="text-sm text-red-400">{x.error}</p>}
              {x.result && <Answer result={x.result} onAsk={ask} disabled={busy} />}
            </div>
          )
        })}
      </div>

      <div ref={bottomRef} className="flex gap-2">
        <input
          value={question}
          onChange={e => setQuestion(e.target.value)}
          onKeyDown={e => e.key === "Enter" && ask()}
          disabled={busy}
          placeholder={exchanges.length ? "Ask another question…" : "e.g. Why did the Church try to control printed books?"}
          className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-4 py-3 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-gray-500 disabled:opacity-50"
        />
        <button
          onClick={() => ask()}
          disabled={busy || !question.trim()}
          className="px-5 bg-white text-gray-950 font-medium text-sm rounded-lg hover:bg-gray-100 transition disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {busy ? "Thinking…" : "Ask"}
        </button>
      </div>
    </div>
  )
}
