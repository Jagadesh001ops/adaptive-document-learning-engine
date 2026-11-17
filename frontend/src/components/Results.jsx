import { useState } from 'react'
import { download } from '../lib/download'
import Quiz from './Quiz'

function renderValue(val) {
  if (val === null || val === undefined) return null
  if (typeof val === 'string') {
    return <p className="text-sm text-gray-300 whitespace-pre-wrap leading-relaxed">{val}</p>
  }
  if (Array.isArray(val)) {
    // quiz items?
    if (val.length && typeof val[0] === 'object' && val[0].question) {
      return <Quiz items={val} />
    }
    // citations?
    if (val.length && typeof val[0] === 'object' && val[0].chunk_id !== undefined) {
      return <CitationList items={val} />
    }
    return (
      <ul className="text-sm text-gray-300 space-y-1 list-disc ml-4">
        {val.map((v, i) => <li key={i}>{typeof v === 'string' ? v : JSON.stringify(v)}</li>)}
      </ul>
    )
  }
  return <pre className="text-xs text-gray-400 whitespace-pre-wrap">{JSON.stringify(val, null, 2)}</pre>
}

function CitationList({ items }) {
  return (
    <div className="space-y-1 mt-1">
      {items.map((c, i) => (
        <div key={i} className="text-xs text-gray-400 bg-gray-800 rounded px-2 py-1 font-mono">
          chunk #{c.chunk_id} (doc {c.document_id}, chars {c.char_start}–{c.char_end})
          {c.score !== undefined && <span className="ml-2 text-gray-500">score={c.score?.toFixed(3)}</span>}
        </div>
      ))}
    </div>
  )
}

export default function Results({ run, onBack }) {
  const [exporting, setExporting] = useState(null)

  if (!run) return null

  const downloadExport = async (format) => {
    setExporting(format)
    try {
      await download(`/runs/${run.run_id}/export/${format}`, `study-kit-${run.run_id}.${format === 'anki' ? 'apkg' : format}`)
    } catch (e) {
      alert(`Export failed: ${e.message}`)
    } finally {
      setExporting(null)
    }
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-2xl font-semibold mb-1">Results</h2>
          <p className="text-gray-400 text-sm">{run.goal}</p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => downloadExport('pdf')}
            disabled={!!exporting}
            className="text-xs px-3 py-1.5 bg-gray-800 border border-gray-700 rounded-md text-gray-300 hover:text-white hover:border-gray-500 transition disabled:opacity-40"
          >
            {exporting === 'pdf' ? 'Exporting…' : 'PDF'}
          </button>
          <button
            onClick={() => downloadExport('docx')}
            disabled={!!exporting}
            className="text-xs px-3 py-1.5 bg-gray-800 border border-gray-700 rounded-md text-gray-300 hover:text-white hover:border-gray-500 transition disabled:opacity-40"
          >
            {exporting === 'docx' ? 'Exporting…' : 'Word'}
          </button>
          <button
            onClick={() => downloadExport('anki')}
            disabled={!!exporting}
            className="text-xs px-3 py-1.5 bg-gray-800 border border-gray-700 rounded-md text-gray-300 hover:text-white hover:border-gray-500 transition disabled:opacity-40"
          >
            {exporting === 'anki' ? 'Exporting…' : 'Anki'}
          </button>
          <button
            onClick={onBack}
            className="text-sm px-3 py-1.5 border border-gray-700 rounded-md text-gray-300 hover:text-white hover:border-gray-500 transition whitespace-nowrap"
          >
            Back
          </button>
        </div>
      </div>

      {/* Metadata */}
      <div className="flex gap-4 text-sm text-gray-400">
        <span>Run #{run.run_id}</span>
        <span>·</span>
        <span>{run.steps?.length} steps</span>
        <span>·</span>
        <span>{run.total_tokens} tokens</span>
        <span>·</span>
        <span>{Math.round(run.total_time_ms)}ms</span>
      </div>

      {/* Pipeline steps */}
      <div className="space-y-3">
        {run.steps?.map((step, i) => {
          const val = run.output?.[step.output_key]
          return (
            <div key={step.name} className="bg-gray-900 border border-gray-800 rounded-lg p-4">
              <div className="flex items-center gap-2 mb-3">
                <span className="text-xs font-mono bg-gray-800 text-gray-400 px-2 py-0.5 rounded">
                  {i + 1}
                </span>
                <span className="text-sm font-medium">{step.name}</span>
                <span className="text-xs text-gray-500 ml-auto font-mono">
                  {step.input_key} → {step.output_key}
                </span>
              </div>
              {val !== undefined && val !== null && renderValue(val)}
            </div>
          )
        })}
      </div>
    </div>
  )
}
