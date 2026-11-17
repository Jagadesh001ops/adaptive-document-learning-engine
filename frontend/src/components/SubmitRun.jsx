import { useEffect, useRef, useState } from "react"
import { apiFetch } from "../lib/api"
import { useNow } from "../lib/useNow"
import { DocumentStatus, RunProgress } from "./RunProgress"

const ACCEPT = ".pdf,.txt,.md,text/plain,application/pdf,text/markdown"
const POLL_MS = 2000

export default function SubmitRun({ onRunComplete, prefillDoc, onClearDoc }) {
  const [file, setFile] = useState(null)
  const [request, setRequest] = useState("")
  const [uploading, setUploading] = useState(false)
  const [streaming, setStreaming] = useState(false)
  const [docState, setDocState] = useState(null)
  const [pipeline, setPipeline] = useState(null)
  const [error, setError] = useState(null)
  const fileInputRef = useRef(null)
  const unmounted = useRef(false)

  useEffect(() => () => { unmounted.current = true }, [])

  // When the user picks a doc from Library, clear any local file selection
  useEffect(() => {
    if (prefillDoc) setFile(null)
  }, [prefillDoc])

  const loading = uploading || streaming
  const now = useNow(uploading || pipeline?.phase === "planning" || pipeline?.phase === "running")

  const updateStep = (name, patch) =>
    setPipeline(p => p && ({ ...p, steps: p.steps.map(s => s.name === name ? { ...s, ...patch } : s) }))

  async function waitUntilReady(id) {
    while (!unmounted.current) {
      const res = await apiFetch(`/documents/${id}`)
      if (!res.ok) throw new Error(`Could not check document status (${res.status})`)
      const doc = await res.json()
      if (doc.status === "ready") return doc
      if (doc.status === "failed") throw new Error(doc.error_message || "Document processing failed")
      setDocState(d => d && { ...d, status: "processing" })
      await new Promise(r => setTimeout(r, POLL_MS))
    }
  }

  async function handleSubmit() {
    if (!file && !prefillDoc) { setError("Please select a file."); return }
    if (!request.trim()) { setError("Please enter a request."); return }
    setError(null)
    setPipeline(null)
    setDocState(null)

    // The backend loads the document's extracted text from document_id.
    let documentId = prefillDoc?.document_id ?? null
    const documentText = `[Document: ${file ? file.name : prefillDoc.filename}]`

    if (file) {
      setUploading(true)
      setDocState({ name: file.name, status: "uploading", startedAt: Date.now() })
      try {
        const form = new FormData()
        form.append("file", file)
        const res = await apiFetch("/documents", { method: "POST", body: form })
        const body = await res.json().catch(() => ({}))
        if (!res.ok) throw new Error(body.detail || `Upload failed: ${res.status}`)
        documentId = body.document_id
        const doc = body.status === "ready" ? null : await waitUntilReady(documentId)
        if (unmounted.current) return
        setDocState(d => ({ ...d, status: "ready", pageCount: doc?.page_count, finishedAt: Date.now() }))
      } catch (err) {
        setDocState(d => d && { ...d, status: "failed", error: err.message, finishedAt: Date.now() })
        return
      } finally {
        setUploading(false)
      }
    }

    setStreaming(true)
    setPipeline({ phase: "planning", startedAt: Date.now(), steps: [] })
    try {
      const res = await apiFetch("/run/stream", {
        method: "POST",
        body: JSON.stringify({
          document: documentText,
          request,
          document_id: documentId,
        }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || `Run failed: ${res.status}`)
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ""

      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split("\n")
        buffer = lines.pop()
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue
          try {
            const event = JSON.parse(line.slice(6))
            if (event.type === "plan") {
              setPipeline(p => ({
                ...p, phase: "running", goal: event.goal,
                steps: event.steps.map(s => ({ ...s, status: "queued" })),
              }))
            } else if (event.type === "step_start") {
              updateStep(event.name, { status: "running", startedAt: Date.now() })
            } else if (event.type === "step") {
              updateStep(event.name, {
                status: event.success ? "done" : "failed",
                time_ms: event.time_ms, tokens: event.tokens, error: event.error,
              })
            } else if (event.type === "done") {
              setPipeline(p => p && { ...p, phase: "done" })
              onRunComplete(event)
            } else if (event.type === "error") {
              setError(event.message)
              setPipeline(p => p && {
                ...p, phase: "failed",
                steps: p.steps.map(s => s.status === "running" ? { ...s, status: "failed" } : s),
              })
            }
          } catch { /* skip malformed SSE lines */ }
        }
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setStreaming(false)
    }
  }

  function handleDrop(e) {
    e.preventDefault()
    const f = e.dataTransfer.files?.[0] ?? null
    if (f) { setFile(f); setError(null); onClearDoc?.() }
  }

  function handleFileChange(e) {
    const f = e.target.files?.[0] ?? null
    setFile(f); setError(null); onClearDoc?.()
  }

  function clearFile() {
    setFile(null)
    if (fileInputRef.current) fileInputRef.current.value = ""
  }

  const activeDoc = file ? { name: file.name, size: `${(file.size/1024).toFixed(1)} KB` }
    : prefillDoc ? { name: prefillDoc.filename, size: `${(prefillDoc.size_bytes/1024).toFixed(1)} KB (library)` }
    : null

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-semibold mb-1">New Run</h2>
        <p className="text-gray-400 text-sm">
          Upload a file or pick one from Library, then describe what you want done.
        </p>
      </div>

      <div className="space-y-4">
        {/* File drop zone */}
        <div>
          <label className="block text-sm font-medium text-gray-300 mb-1.5">Document</label>
          <div
            onClick={() => !activeDoc && fileInputRef.current?.click()}
            onDrop={handleDrop}
            onDragOver={e => e.preventDefault()}
            className={`w-full border-2 border-dashed rounded-lg px-4 py-8 text-center transition
              ${activeDoc
                ? "border-gray-600 bg-gray-900 cursor-default"
                : "border-gray-700 bg-gray-900 hover:border-gray-500 cursor-pointer"}`}
          >
            <input ref={fileInputRef} type="file" accept={ACCEPT}
              className="hidden" onChange={handleFileChange} />
            {activeDoc ? (
              <div className="space-y-1">
                <p className="text-sm text-white font-medium">{activeDoc.name}</p>
                <p className="text-xs text-gray-400">
                  {activeDoc.size} &middot;{" "}
                  <span className="text-gray-500 hover:text-gray-300 cursor-pointer transition"
                    onClick={e => { e.stopPropagation(); clearFile(); onClearDoc?.() }}>
                    Remove
                  </span>
                </p>
              </div>
            ) : (
              <div className="space-y-1">
                <p className="text-sm text-gray-400">
                  Drop a file here or <span className="text-white underline">browse</span>
                </p>
                <p className="text-xs text-gray-600">PDF, TXT, MD — up to 25 MB</p>
              </div>
            )}
          </div>
        </div>

        {/* Request */}
        <div>
          <label className="block text-sm font-medium text-gray-300 mb-1.5">Request</label>
          <input
            value={request}
            onChange={e => setRequest(e.target.value)}
            onKeyDown={e => e.key === "Enter" && !loading && handleSubmit()}
            placeholder="e.g. Summarize this, extract key concepts, and generate quiz questions."
            className="w-full bg-gray-900 border border-gray-700 rounded-lg px-4 py-3 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-gray-500"
          />
        </div>

        {docState && <DocumentStatus doc={docState} now={now} />}
        {pipeline && <RunProgress pipeline={pipeline} now={now} />}

        {error && <p className="text-red-400 text-sm">{error}</p>}

        <button
          onClick={handleSubmit}
          disabled={loading}
          className="w-full bg-white text-gray-950 font-medium text-sm py-3 rounded-lg hover:bg-gray-100 transition disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {uploading ? "Preparing document…" : streaming ? "Running workflow…" : "Run Workflow"}
        </button>
      </div>
    </div>
  )
}
