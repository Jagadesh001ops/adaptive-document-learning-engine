import { useEffect, useState } from "react"

// Current time, re-rendered every 200ms while `active`, for live elapsed timers.
export function useNow(active) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const id = setInterval(() => setNow(Date.now()), 200)
    return () => clearInterval(id)
  }, [active])
  return now
}
