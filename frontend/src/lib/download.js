import { apiFetch } from "./api"

// Fetch a file from the API and save it, using the server's filename when it sends one.
export async function download(path, fallbackName) {
  const res = await apiFetch(path)
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `Download failed: ${res.status}`)
  }
  const match = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "")
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = match?.[1] || fallbackName
  a.click()
  URL.revokeObjectURL(url)
}
