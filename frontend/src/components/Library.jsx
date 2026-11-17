import { useEffect, useState } from 'react'
import { apiFetch } from '../lib/api'

// How far each document has been studied, and the one thing to do next.

const SEGMENT = {
  new: 'bg-gray-800',
  read: 'bg-sky-800',
  quizzed: 'bg-sky-600',
  passed: 'bg-emerald-500',
  missed: 'bg-amber-400',
}

const STATUS_TEXT = {
  new: 'not started',
  read: 'read, not tested',
  quizzed: 'quiz not answered yet',
  passed: 'tested, all right',
  missed: 'tested, some missed',
}

const tone = s => (s.status === 'tested' ? (s.wrong ? 'missed' : 'passed') : s.status)

function plural(n, word) {
  return `${n} ${word}${n === 1 ? '' : 's'}`
}

function SectionMap({ sections }) {
  return (
    <div className="space-y-1.5">
      <div className="flex gap-1" role="list" aria-label="Sections">
        {sections.map(s => (
          <div
            key={s.section}
            role="listitem"
            title={`${s.section}. ${s.title}${s.pages ? ` · pp. ${s.pages[0]}–${s.pages[1]}` : ''} · ${STATUS_TEXT[tone(s)]}`}
            className={`h-2 flex-1 rounded-sm ${SEGMENT[tone(s)]}`}
          />
        ))}
      </div>
      <div className="flex gap-1 text-[10px] leading-tight text-gray-600">
        {sections.map(s => (
          <span key={s.section} className="flex-1 truncate" title={s.title}>{s.title}</span>
        ))}
      </div>
    </div>
  )
}

function doneLine(done) {
  const parts = []
  if (done.summaries) parts.push(plural(done.summaries, 'summary').replace('summarys', 'summaries'))
  if (done.concepts) parts.push(plural(done.concepts, 'concept list'))
  if (done.questions_asked) parts.push(plural(done.questions_asked, 'question') + ' asked')
  if (done.quizzes) parts.push(plural(done.quizzes, 'quiz').replace('quizs', 'quizzes'))
  if (done.answered) parts.push(`${done.correct}/${done.answered} answers right`)
  return parts.join(' · ')
}

function Progress({ doc, onStartTask }) {
  const [state, setState] = useState({ loading: true })

  useEffect(() => {
    let cancelled = false
    apiFetch(`/documents/${doc.document_id}/progress`)
      .then(async r => {
        const body = await r.json().catch(() => ({}))
        if (!r.ok) throw new Error(body.detail || `${r.status}`)
        return body
      })
      .then(p => !cancelled && setState({ progress: p }))
      .catch(err => !cancelled && setState({ error: err.message }))
    return () => { cancelled = true }
  }, [doc.document_id])

  if (state.loading) {
    return <p className="text-xs text-gray-500 font-mono">mapping the document's sections…</p>
  }
  if (state.error) return <p className="text-xs text-red-400">Couldn't load progress: {state.error}</p>

  const { sections, done, next, latest_session_id: latest } = state.progress
  const studied = sections.filter(s => s.status !== 'new').length
  const summary = doneLine(done)

  return (
    <div className="space-y-3">
      {sections.length > 0 && <SectionMap sections={sections} />}
      <p className="text-xs text-gray-400">
        {studied} of {sections.length} sections studied
        {summary && <span className="text-gray-500"> · {summary}</span>}
      </p>
      <button
        onClick={() => onStartTask(doc, { ...next, session_id: next.session_id ?? latest })}
        className="group w-full text-left rounded-lg border border-gray-700 hover:border-sky-600 bg-gray-950/40 px-3 py-2.5 transition"
      >
        <span className="block text-[10px] uppercase tracking-wider text-gray-500">Next</span>
        <span className="block text-sm text-white group-hover:text-sky-300 transition">
          {next.label} <span aria-hidden>→</span>
        </span>
        <span className="block text-xs text-gray-500">{next.detail}</span>
      </button>
    </div>
  )
}

export default function Library({ onStartTask }) {
  const [docs, setDocs] = useState([])
  const [loading, setLoading] = useState(true)
  const [deleting, setDeleting] = useState(null)

  const load = async ({ silent = false } = {}) => {
    if (!silent) setLoading(true)
    try {
      const res = await apiFetch("/documents")
      if (res.ok) setDocs(await res.json())
    } catch { /* ignore fetch errors */ }
    if (!silent) setLoading(false)
  }

  useEffect(() => { load() }, [])

  // Keep refreshing while anything is still being processed.
  const anyProcessing = docs.some(d => d.status === 'processing')
  useEffect(() => {
    if (!anyProcessing) return
    const id = setInterval(() => load({ silent: true }), 3000)
    return () => clearInterval(id)
  }, [anyProcessing])

  const deleteDoc = async (id) => {
    if (!confirm('Delete this document and its study sessions?')) return
    setDeleting(id)
    try {
      await apiFetch(`/documents/${id}`, { method: 'DELETE' })
      setDocs(d => d.filter(x => x.document_id !== id))
    } catch { /* ignore fetch errors */ }
    setDeleting(null)
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-semibold">Library</h2>
        <button onClick={load} className="text-xs text-gray-400 hover:text-gray-200 transition">
          Refresh
        </button>
      </div>

      {loading ? (
        <p className="text-gray-500 text-sm">Loading…</p>
      ) : docs.length === 0 ? (
        <p className="text-gray-500 text-sm">No documents yet. Upload one from the Study tab.</p>
      ) : (
        <div className="space-y-3">
          {docs.map(doc => (
            <div key={doc.document_id} className="bg-gray-900 border border-gray-800 rounded-lg p-4 space-y-3">
              <div className="flex items-start gap-3">
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium truncate">{doc.filename}</p>
                  <p className="text-xs text-gray-500 mt-0.5">
                    {doc.page_count} p · {(doc.size_bytes / 1024).toFixed(1)} KB
                    {doc.status !== 'ready' && (
                      <span className={doc.status === 'failed' ? 'text-red-400' : 'text-amber-300'}>
                        {' · '}{doc.status === 'processing' ? 'processing — building search index, available shortly' : doc.status}
                      </span>
                    )}
                  </p>
                  {doc.status === 'failed' && doc.error_message && (
                    <p className="text-xs text-red-400/80 mt-0.5 truncate" title={doc.error_message}>{doc.error_message}</p>
                  )}
                </div>
                <button
                  onClick={() => deleteDoc(doc.document_id)}
                  disabled={deleting === doc.document_id}
                  className="shrink-0 text-xs text-gray-600 hover:text-red-400 transition disabled:opacity-40"
                >
                  {deleting === doc.document_id ? '…' : 'Delete'}
                </button>
              </div>
              {doc.status === 'ready' && <Progress doc={doc} onStartTask={onStartTask} />}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
