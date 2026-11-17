const seconds = ms => `${(ms / 1000).toFixed(1)}s`

const RULE = {
  queued: "border-l-gray-800",
  running: "border-l-amber-400",
  done: "border-l-emerald-500",
  failed: "border-l-red-500",
}

function Row({ index, name, flow, status, detail, error }) {
  return (
    <li className={`border-l-2 ${RULE[status]} px-4 py-2.5`}>
      <div className="flex items-baseline gap-4">
        <span className="text-gray-600 tabular-nums">{String(index).padStart(2, "0")}</span>
        <span className={status === "queued" ? "text-gray-500" : "text-gray-100"}>{name}</span>
        {flow && <span className="hidden sm:inline text-gray-600">{flow}</span>}
        <span className={`ml-auto tabular-nums ${status === "running" ? "text-amber-300" : "text-gray-500"}`}>
          {detail}
        </span>
      </div>
      {error && <p className="mt-1 pl-9 text-red-400 whitespace-pre-wrap">{error}</p>}
    </li>
  )
}

export function DocumentStatus({ doc, now }) {
  const elapsed = (doc.finishedAt ?? now) - doc.startedAt
  const text = {
    uploading: `uploading and extracting text · ${seconds(elapsed)}`,
    processing: `building the search index · ${seconds(elapsed)}`,
    ready: `ready${doc.pageCount ? ` · ${doc.pageCount} pages` : ""} · ${seconds(elapsed)}`,
    failed: "processing failed",
  }[doc.status]
  const status = { uploading: "running", processing: "running", ready: "done", failed: "failed" }[doc.status]

  return (
    <ol className="border border-gray-800 rounded-lg font-mono text-xs">
      <Row index={0} name={doc.name} status={status} detail={text} error={doc.error} />
    </ol>
  )
}

export function RunProgress({ pipeline, now }) {
  const planning = pipeline.phase === "planning"
  const planDetail = planning
    ? `working out the steps · ${seconds(now - pipeline.startedAt)}`
    : `${pipeline.steps.length} step${pipeline.steps.length === 1 ? "" : "s"}`

  return (
    <div className="space-y-2">
      {pipeline.goal && <p className="text-xs text-gray-400">{pipeline.goal}</p>}
      <ol className="border border-gray-800 rounded-lg divide-y divide-gray-800 font-mono text-xs">
        <Row index={0} name="plan" status={planning ? "running" : "done"} detail={planDetail} />
        {pipeline.steps.map((s, i) => (
          <Row
            key={s.name}
            index={i + 1}
            name={s.name}
            flow={`${s.input_key} → ${s.output_key}`}
            status={s.status}
            error={s.error}
            detail={
              s.status === "running" ? seconds(now - s.startedAt)
                : s.status === "done" ? `${seconds(s.time_ms)} · ${s.tokens} tok`
                : s.status
            }
          />
        ))}
      </ol>
    </div>
  )
}
