// Read a server-sent-events response body, calling onEvent for each parsed `data:` line.
export async function readEvents(res, onEvent) {
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split("\n")
    buffer = lines.pop()
    for (const line of lines) {
      if (!line.startsWith("data: ")) continue
      let event
      try { event = JSON.parse(line.slice(6)) } catch { continue }
      onEvent(event)
    }
  }
}
