import { useEffect, useState } from 'react'
import { api } from '../api.js'

function blankPerson() {
  return { display_name: '', identifier: '' }
}

function normalizedPerson(person) {
  return {
    display_name: person?.display_name || '',
    identifier: person?.identifier || '',
  }
}

export default function PeopleSettings({ compact = false }) {
  const [people, setPeople] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [editing, setEditing] = useState(null)
  const [draft, setDraft] = useState(blankPerson)
  const [busy, setBusy] = useState(false)

  const load = async () => {
    setLoading(true); setError('')
    try { const response = await api.people(); setPeople(response.people || []) }
    catch { setError('People could not load. Try again; existing work remains in its local board.') }
    finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  const startNew = () => { setEditing('new'); setDraft(blankPerson()); setError('') }
  const startEdit = (person) => { setEditing(person.id); setDraft(normalizedPerson(person)); setError('') }
  const cancel = () => { setEditing(null); setDraft(blankPerson()) }
  const save = async (event) => {
    event.preventDefault()
    if (busy || !draft.display_name.trim() || !draft.identifier.trim()) return
    setBusy(true); setError('')
    const payload = { display_name: draft.display_name.trim(), identifier: draft.identifier.trim() }
    try {
      if (editing === 'new') await api.savePerson(payload)
      else await api.editPerson(editing, payload)
      cancel(); await load()
    } catch { setError('Could not save this person. Each identity can belong to only one person.') }
    finally { setBusy(false) }
  }
  const remove = async (person) => {
    if (busy || person.is_self || !window.confirm(`Remove ${person.display_name}? Existing work will remain under Others if it is still linked.`)) return
    setBusy(true); setError('')
    try { await api.deletePerson(person.id); await load() }
    catch { setError('Could not remove this person. Try again.') }
    finally { setBusy(false) }
  }

  return <section className={`settings-section people-settings${compact ? ' settings-compact' : ''}`} aria-labelledby="people-settings-title">
    <div className="settings-section-heading"><div><p className="eyebrow">People</p><h2 id="people-settings-title">People you explicitly track</h2><p>Only people you add here receive a Team lane. Watson keeps everyone else under Others without suggesting them here.</p></div><button type="button" onClick={startNew} disabled={busy}>Add person</button></div>
    <p className="settings-limit-note">Lane order follows the backend’s configured order (lane_position). Watson does not pretend a drag reorder is saved when no ordering API exists.</p>
    {error && <p className="settings-error" role="alert">{error} <button type="button" onClick={load} disabled={busy}>Retry</button></p>}
    {loading ? <p className="settings-muted">Loading people…</p> : <div className="people-list">{people.map((person) => <article className="person-settings-card" key={person.id}><div><h3>{person.display_name} {person.is_self && <span className="person-self">You</span>}</h3><p>{person.is_self ? 'Your profile' : `@${person.identifier} · lane ${person.lane_position}`}</p></div><div className="person-settings-actions">{!person.is_self && <><button type="button" onClick={() => startEdit(person)} disabled={busy}>Edit</button><button className="settings-danger" type="button" onClick={() => remove(person)} disabled={busy}>Remove</button></>}</div></article>)}</div>}
    {editing !== null && <form className="person-editor" onSubmit={save}><div className="settings-section-heading"><h3>{editing === 'new' ? 'Add a person' : 'Edit person'}</h3><button type="button" onClick={cancel} disabled={busy}>Cancel</button></div><label>Display name<input value={draft.display_name} onChange={(event) => setDraft((current) => ({ ...current, display_name: event.target.value }))} disabled={busy} required /></label><label>Identifier<input value={draft.identifier} onChange={(event) => setDraft((current) => ({ ...current, identifier: event.target.value }))} placeholder="alex.dev" pattern="[A-Za-z0-9][A-Za-z0-9._-]*" disabled={busy} required /><span className="settings-muted">Used as the GitLab username and combined with your profile email domain for ClickUp and Calendar.</span></label><div className="settings-form-actions"><button className="settings-primary" type="submit" disabled={busy}>{busy ? 'Saving…' : 'Save person'}</button></div></form>}
  </section>
}
