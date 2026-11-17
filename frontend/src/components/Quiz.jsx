import { useState } from "react"

// A multiple-choice quiz the student can answer and check. Items: {question, options, answer,
// explanation, verified?, citation?: {chunk_id, excerpt}}. `saved` holds earlier answers keyed by
// question number (from 1); `onSubmit(answers)` stores new ones and may throw.
export default function Quiz({ items, changed = null, saved = null, onSubmit = null }) {
  const [selected, setSelected] = useState(() =>
    saved ? Object.fromEntries(Object.entries(saved).map(([n, letter]) => [Number(n) - 1, letter])) : {})
  const [submitted, setSubmitted] = useState(() => !!saved && Object.keys(saved).length >= (items?.length || 0))
  const [saveError, setSaveError] = useState(null)

  async function check() {
    setSubmitted(true)
    setSaveError(null)
    if (!onSubmit) return
    try {
      await onSubmit(Object.fromEntries(Object.entries(selected).map(([i, letter]) => [Number(i) + 1, letter])))
    } catch (err) {
      setSaveError(`Your answers weren't saved: ${err.message}`)
    }
  }

  if (!items?.length) return <p className="text-sm text-gray-500">No questions came back.</p>

  const score = items.reduce((n, item, i) => n + (selected[i] === item.answer ? 1 : 0), 0)

  return (
    <div className="space-y-4">
      {items.map((item, qi) => {
        const chose = selected[qi]
        return (
          <div
            key={qi}
            className={`border rounded-lg p-3 ${changed === qi + 1 ? "border-sky-700/70" : "border-gray-800"}`}
          >
            <p className="text-sm font-medium mb-2">
              <span className="text-gray-500 tabular-nums">{qi + 1}.</span> {item.question}
              {changed === qi + 1 && <span className="ml-2 text-xs text-sky-400">changed</span>}
            </p>
            <div className="space-y-1">
              {Object.entries(item.options || {}).map(([letter, text]) => {
                let cls = "w-full text-left border rounded px-3 py-1.5 text-sm flex gap-2 transition "
                if (!submitted) {
                  cls += chose === letter
                    ? "border-sky-500 bg-sky-900/30 text-white"
                    : "border-gray-700 text-gray-300 hover:border-gray-500"
                } else if (letter === item.answer) cls += "border-emerald-500 bg-emerald-900/20 text-emerald-300"
                else if (letter === chose) cls += "border-red-500 bg-red-900/20 text-red-300"
                else cls += "border-gray-800 text-gray-500"
                return (
                  <button
                    key={letter}
                    disabled={submitted}
                    className={cls}
                    onClick={() => setSelected(s => ({ ...s, [qi]: letter }))}
                  >
                    <span className="font-mono font-bold">{letter}.</span> {text}
                  </button>
                )
              })}
            </div>
            {submitted && (
              <div className="mt-2 space-y-1">
                {item.explanation && <p className="text-xs text-gray-400">{item.explanation}</p>}
                {item.citation?.excerpt && (
                  <p className="text-xs text-gray-500 border-l-2 border-gray-700 pl-2">
                    “{item.citation.excerpt}”
                  </p>
                )}
              </div>
            )}
            {item.verified === false && (
              <p className="mt-2 text-xs text-amber-300">
                Couldn't find this question's quote in the document. Treat it with care.
              </p>
            )}
          </div>
        )
      })}

      {!submitted ? (
        <button
          onClick={check}
          disabled={Object.keys(selected).length < items.length}
          className="text-sm px-4 py-2 bg-sky-600 hover:bg-sky-500 disabled:opacity-40 rounded-md text-white transition"
        >
          Check answers
        </button>
      ) : (
        <p className="text-sm font-medium text-emerald-400">
          {score} of {items.length} right
          <button
            onClick={() => { setSelected({}); setSubmitted(false) }}
            className="ml-3 text-xs font-normal text-gray-500 hover:text-gray-300"
          >
            try again
          </button>
        </p>
      )}
      {saveError && <p className="text-xs text-amber-300">{saveError}</p>}
    </div>
  )
}
