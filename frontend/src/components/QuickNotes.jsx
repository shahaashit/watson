import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'

export function NotesToggle({ open, onToggle }) {
  return <button type="button" className={`notes-toggle${open ? ' active' : ''}`} aria-pressed={open} onClick={onToggle}>
    <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 3.5h10v13H5zM7.5 7h5M7.5 10h5M7.5 13h3" /></svg>
    <span>Notes</span>
  </button>
}

// Scratch notes beside the work boards. Notes are stored locally by Watson and
// never enter the capture pipeline, so nothing here is classified or synced.
export default function QuickNotes({ onClose }) {
  const [notes, setNotes] = useState([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(() => api.notes().then((data) => {
    setNotes(data.notes || []); setError('')
  }).catch(() => setError('Could not load notes. Retry when ready.')), [])
  useEffect(() => { load() }, [load])

  const add = async (event) => {
    event.preventDefault()
    const body = draft.trim()
    if (!body || busy) return
    setBusy(true); setError('')
    try {
      const { note } = await api.createNote(body)
      setNotes((current) => [note, ...current])
      setDraft('')
    } catch { setError('Could not save the note. Try again.') } finally { setBusy(false) }
  }
  const save = async (note, value) => {
    const body = value.trim()
    if (!body || body === note.body) return
    try {
      const saved = await api.updateNote(note.id, body)
      setNotes((current) => current.map((entry) => entry.id === note.id ? saved.note : entry))
      setError('')
    } catch { setError('Could not update the note. Try again.') }
  }
  const remove = async (note) => {
    try {
      await api.deleteNote(note.id)
      setNotes((current) => current.filter((entry) => entry.id !== note.id))
      setError('')
    } catch { setError('Could not delete the note. Try again.') }
  }
  const draftKeyDown = (event) => {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) add(event)
  }

  return <aside className="notes-panel" aria-label="Quick notes">
    <header><h2>Quick notes</h2><button type="button" onClick={onClose} aria-label="Hide quick notes">×</button></header>
    <form onSubmit={add}>
      <label className="sr-only" htmlFor="quick-note-draft">New note</label>
      <textarea id="quick-note-draft" rows="3" value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={draftKeyDown} placeholder="Jot something down…" />
      <button type="submit" disabled={busy || !draft.trim()}>{busy ? 'Saving…' : 'Add note'}</button>
    </form>
    {error && <p className="work-inline-error" role="alert">{error}</p>}
    {notes.length ? <ul className="notes-list">
      {notes.map((note) => <li key={note.id}>
        <textarea aria-label={`Note from ${new Date(note.created_at).toLocaleString()}`} defaultValue={note.body} onBlur={(event) => save(note, event.target.value)} />
        <button type="button" onClick={() => remove(note)} aria-label="Delete note">×</button>
      </li>)}
    </ul> : <p className="notes-empty">No notes yet. They stay here until you delete them.</p>}
  </aside>
}
