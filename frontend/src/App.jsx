import { useState } from "react"
// import { supabase } from "./lib/supabase"
// import Auth from "./components/Auth"
import Results from "./components/Results"
import RunHistory from "./components/RunHistory"
import Library from "./components/Library"
import Search from "./components/Search"
import Study from "./components/Study"

const TABS = [
  { id: "study",   label: "Study" },
  { id: "library", label: "Library" },
  { id: "search",  label: "Search" },
  { id: "history", label: "History" },
]

export default function App() {
  // const [session, setSession] = useState(undefined)
  const [screen, setScreen] = useState("study")
  const [currentRun, setCurrentRun] = useState(null)
  const [studyTask, setStudyTask] = useState(null)

  // Auth disabled temporarily — uncomment to re-enable
  // useEffect(() => {
  //   supabase.auth.getSession().then(({ data: { session } }) => setSession(session))
  //   const { data: { subscription } } = supabase.auth.onAuthStateChange((_e, session) => {
  //     setSession(session)
  //   })
  //   return () => subscription.unsubscribe()
  // }, [])
  // if (session === undefined) return null
  // if (!session) return <Auth />

  // A suggested task picked in Library opens Study and runs it.
  function startTask(doc, next) {
    setStudyTask({ doc, message: next?.message ?? null, session_id: next?.session_id ?? null,
                   turn: next?.turn ?? null, id: Date.now() })
    setScreen("study")
  }

  return (
    <div className="min-h-screen bg-gray-950 text-white">
      <nav className="border-b border-gray-800 px-6 py-4 flex items-center justify-between">
        <h1 className="text-lg font-semibold tracking-tight">Adaptive Document Learning Engine</h1>
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
        {screen === "study" && <Study task={studyTask} onTask={setStudyTask} />}
        {screen === "results" && (
          <Results run={currentRun} onBack={() => setScreen("history")} />
        )}
        {screen === "library" && (
          <Library onStartTask={startTask} />
        )}
        {screen === "search" && <Search />}
        {screen === "history" && (
          <RunHistory onSelectRun={(run) => { setCurrentRun(run); setScreen("results") }} />
        )}
      </main>
    </div>
  )
}
