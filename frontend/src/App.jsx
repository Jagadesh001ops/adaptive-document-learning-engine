import { useState, useEffect } from "react"
import { supabase } from "./lib/supabase"
import Auth from "./components/Auth"
import SubmitRun from "./components/SubmitRun"
import Results from "./components/Results"
import RunHistory from "./components/RunHistory"
import Library from "./components/Library"
import Search from "./components/Search"

const TABS = [
  { id: "submit",  label: "New Run" },
  { id: "library", label: "Library" },
  { id: "search",  label: "Search" },
  { id: "history", label: "History" },
]

export default function App() {
  const [session, setSession] = useState(undefined)
  const [screen, setScreen] = useState("submit")
  const [currentRun, setCurrentRun] = useState(null)
  const [selectedDoc, setSelectedDoc] = useState(null)

  useEffect(() => {
    supabase.auth.getSession().then(({ data: { session } }) => setSession(session))
    const { data: { subscription } } = supabase.auth.onAuthStateChange((_e, session) => {
      setSession(session)
    })
    return () => subscription.unsubscribe()
  }, [])

  // if (session === undefined) return null
  // if (!session) return <Auth />

  function handleRunComplete(run) {
    setCurrentRun(run)
    setScreen("results")
  }

  function handleSelectDoc(doc) {
    setSelectedDoc(doc)
    setScreen("submit")
  }

  return (
    <div className="min-h-screen bg-gray-950 text-white">
      <nav className="border-b border-gray-800 px-6 py-4 flex items-center justify-between">
        <h1 className="text-lg font-semibold tracking-tight">AI Workflow Engine</h1>
        <div className="flex items-center gap-3">
          <div className="flex gap-1">
            {TABS.map(t => (
              <button
                key={t.id}
                onClick={() => setScreen(t.id)}
                className={`text-sm px-3 py-1.5 rounded-md transition ${
                  screen === t.id
                    ? "bg-gray-800 text-white"
                    : "text-gray-400 hover:text-white"
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
          {/* Sign out button hidden while auth is disabled */}
        </div>
      </nav>

      <main className="max-w-3xl mx-auto px-6 py-10">
        {screen === "submit" && (
          <SubmitRun
            onRunComplete={handleRunComplete}
            prefillDoc={selectedDoc}
            onClearDoc={() => setSelectedDoc(null)}
          />
        )}
        {screen === "results" && (
          <Results run={currentRun} onNewRun={() => setScreen("submit")} />
        )}
        {screen === "library" && (
          <Library onSelectDoc={handleSelectDoc} />
        )}
        {screen === "search" && <Search />}
        {screen === "history" && (
          <RunHistory onSelectRun={(run) => { setCurrentRun(run); setScreen("results") }} />
        )}
      </main>
    </div>
  )
}
