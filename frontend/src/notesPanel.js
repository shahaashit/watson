import { useEffect, useRef, useState } from 'react'

const NOTES_OPEN_STORAGE_KEY = 'watson.notes.open'

export function readNotesOpen(storage = typeof localStorage === 'undefined' ? null : localStorage) {
  try { return storage?.getItem(NOTES_OPEN_STORAGE_KEY) === 'true' }
  catch { return false }
}

export function writeNotesOpen(open, storage = typeof localStorage === 'undefined' ? null : localStorage) {
  try { storage?.setItem(NOTES_OPEN_STORAGE_KEY, open ? 'true' : 'false') }
  catch { /* private browsing or unavailable storage */ }
}

// The panel stays mounted for the length of its closing slide so the exit
// animation can run; NOTES_TRANSITION_MS must match the CSS duration.
export const NOTES_TRANSITION_MS = 200

export function useNotesPanel() {
  const [open, setOpen] = useState(() => readNotesOpen())
  const [mounted, setMounted] = useState(open)
  const timer = useRef(null)

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])

  const toggle = () => {
    const next = !open
    writeNotesOpen(next)
    setOpen(next)
    if (timer.current) clearTimeout(timer.current)
    if (next) setMounted(true)
    else timer.current = setTimeout(() => setMounted(false), NOTES_TRANSITION_MS)
  }

  return { open, mounted, toggle }
}
