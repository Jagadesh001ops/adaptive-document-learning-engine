import Quiz from "./Quiz"

const VERIFICATION = {
  checked: { text: "Checked against the passages", tone: "text-emerald-400" },
  revised: { text: "Corrected after re-reading", tone: "text-emerald-400" },
  unverified: { text: "Couldn't fully verify this answer. Treat it with care", tone: "text-amber-300" },
  unchecked: { text: "Not checked", tone: "text-amber-300" },
}

const Label = ({ children }) => (
  <p className="text-xs uppercase tracking-wide text-gray-500 mb-1">{children}</p>
)

// **bold** inside a line.
function Inline({ text }) {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**")
      ? <strong key={i} className="text-white font-medium">{part.slice(2, -2)}</strong>
      : part)
}

// Paragraphs, bullet lists and numbered lists from plain model text.
export function Prose({ text }) {
  const groups = []
  for (const raw of (text || "").split("\n")) {
    const line = raw.trim()
    const bullet = /^[-*•]\s+(.*)/.exec(line)
    const numbered = /^\d+[.)]\s+(.*)/.exec(line)
    const kind = bullet ? "ul" : numbered ? "ol" : line ? "p" : null
    if (!kind) { groups.push(null); continue }
    const content = (bullet || numbered)?.[1] ?? line.replace(/^#+\s*/, "")
    const last = groups[groups.length - 1]
    if (last && last.kind === kind && kind !== "p") last.items.push(content)
    else groups.push({ kind, items: [content] })
  }
  return (
    <div className="space-y-2 text-sm text-gray-200 leading-relaxed">
      {groups.filter(Boolean).map((g, i) => {
        if (g.kind === "p") return <p key={i}><Inline text={g.items[0]} /></p>
        const List = g.kind
        return (
          <List key={i} className={`${g.kind === "ul" ? "list-disc" : "list-decimal"} pl-5 space-y-1`}>
            {g.items.map((it, j) => <li key={j}><Inline text={it} /></li>)}
          </List>
        )
      })}
    </div>
  )
}

function Answer({ block, onAsk, disabled }) {
  const v = VERIFICATION[block.verification] ?? VERIFICATION.unchecked
  return (
    <div className="space-y-4">
      {block.status === "not_in_document" && <Label>The document doesn't cover this directly</Label>}
      <Prose text={block.answer} />
      {block.justification && (
        <div>
          <Label>Why</Label>
          <p className="text-sm text-gray-400 leading-relaxed whitespace-pre-wrap">{block.justification}</p>
        </div>
      )}
      {block.citations?.length > 0 && (
        <div className="space-y-2">
          <Label>From the document</Label>
          {block.citations.map((c, i) => (
            <blockquote key={i} className="border-l-2 border-gray-700 pl-3 text-sm text-gray-300">
              “{c.quote}”
              <span className="block text-xs text-gray-600 font-mono mt-0.5">passage {c.chunk_id}</span>
            </blockquote>
          ))}
        </div>
      )}
      {block.general_knowledge && (
        <div className="rounded-lg border border-gray-800 px-3 py-2">
          <Label>Beyond the document</Label>
          <p className="text-sm text-gray-400 whitespace-pre-wrap">{block.general_knowledge}</p>
        </div>
      )}
      <p className={`text-xs ${v.tone}`}>
        {v.text}
        {block.verification === "revised" && ` (${block.revisions}×)`}
        {block.passages_read ? <span className="text-gray-600"> · {block.passages_read} passages read</span> : null}
      </p>
      {block.follow_ups?.length > 0 && onAsk && (
        <div className="flex flex-wrap gap-2">
          {block.follow_ups.map(q => (
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

const TITLES = { answer: "Answer", summary: "Summary", concepts: "Key concepts", quiz: "Quiz" }

function revisedLabel(block) {
  const from = block.revised_from
  if (!from) return null
  const what = from.item ? `question ${from.item}` : (TITLES[block.type] || "output").toLowerCase()
  return `Changed ${what}${from.turn ? ` from turn ${from.turn}` : ""}`
}

// One output of a study turn. Questions are numbered per quiz, as the planner sees them, so
// "make question 2 easier" means the question the student sees as 2.
export function Block({ block, onAsk, disabled }) {
  if (block.type === "note") return <p className="text-sm text-gray-400 italic">{block.text}</p>
  const revised = revisedLabel(block)
  return (
    <section className="space-y-2">
      <div className="flex items-baseline gap-3">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-400">
          {TITLES[block.type] || block.type}
        </h3>
        {block.type === "quiz" && block.difficulty && (
          <span className="text-xs text-gray-600">{block.difficulty}</span>
        )}
        {revised && <span className="text-xs text-sky-400">{revised}</span>}
      </div>
      {block.type === "answer" && <Answer block={block} onAsk={onAsk} disabled={disabled} />}
      {(block.type === "summary" || block.type === "concepts") && <Prose text={block.text} />}
      {block.type === "quiz" && (
        <Quiz items={block.items} changed={block.revised_from?.item ?? null} />
      )}
    </section>
  )
}
