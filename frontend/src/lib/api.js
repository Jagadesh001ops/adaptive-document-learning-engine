import { supabase } from "./supabase"

// Production builds are served by FastAPI, so they call their own origin unless overridden.
const BASE = import.meta.env.VITE_API_URL || (import.meta.env.PROD ? "" : "http://localhost:8000")

export async function apiFetch(path, options = {}) {
  const session = supabase ? (await supabase.auth.getSession()).data.session : null
  const token = session?.access_token

  const headers = { ...(options.headers || {}) }
  if (token) headers["Authorization"] = `Bearer ${token}`
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json"
  }

  const res = await fetch(`${BASE}${path}`, { ...options, headers })
  return res
}
