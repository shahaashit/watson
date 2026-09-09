import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'

const id = (value) => value == null ? '' : String(value)
const contains = (items, value) => items.some((item) => id(item.id) === value)

export default function ClickUpDestination({ integration = {}, onChanged }) {
  const [workspaces, setWorkspaces] = useState([])
  const [spaces, setSpaces] = useState([])
  const [lists, setLists] = useState([])
  const [selection, setSelection] = useState({ workspace_id: '', space_id: '', list_id: '' })
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const generation = useRef(0)
  const savingRef = useRef(false)

  // Every load owns a generation, including prop changes and retries. Late
  // responses must never replace options belonging to a newer selection.
  const load = async (level, requested) => {
    const current = ++generation.current
    const active = () => current === generation.current
    setLoading(true); setError(''); setMessage('')
    let next = { ...requested }
    try {
      if (level === 0) {
        const result = await api.clickupWorkspaces()
        if (!active()) return
        setWorkspaces(result.workspaces)
        const workspace = next.workspace_id || id(result.selected_workspace_id)
        next.workspace_id = contains(result.workspaces, workspace) ? workspace : ''
        if (next.workspace_id !== requested.workspace_id) {
          next.space_id = ''; next.list_id = ''
        }
      }
      if (level <= 1) {
        const result = next.workspace_id ? await api.clickupSpaces(next.workspace_id) : { spaces: [] }
        if (!active()) return
        setSpaces(result.spaces)
        if (!contains(result.spaces, next.space_id)) { next.space_id = ''; next.list_id = '' }
      }
      const result = next.workspace_id && next.space_id
        ? await api.clickupLists(next.workspace_id, next.space_id) : { lists: [] }
      if (!active()) return
      setLists(result.lists)
      if (!contains(result.lists, next.list_id)) next.list_id = ''
      setSelection(next)
    } catch {
      if (active()) setError('Could not load ClickUp destinations. Try again.')
    } finally {
      if (active()) setLoading(false)
    }
  }

  useEffect(() => {
    const initial = {
      workspace_id: id(integration.workspace_id),
      space_id: id(integration.space_id),
      list_id: id(integration.create_list_id),
    }
    setSelection(initial); setWorkspaces([]); setSpaces([]); setLists([])
    setSaving(false); savingRef.current = false
    load(0, initial)
    return () => { generation.current += 1 }
  }, [integration.workspace_id, integration.space_id, integration.create_list_id])

  const busy = loading || saving
  const valid = contains(workspaces, selection.workspace_id)
    && contains(spaces, selection.space_id) && contains(lists, selection.list_id)
  const save = async (event) => {
    event.preventDefault()
    if (busy || !valid || error || savingRef.current) return
    savingRef.current = true
    const current = ++generation.current
    setSaving(true); setMessage('')
    try {
      await api.saveClickupDestination(selection)
    } catch {
      if (current === generation.current) {
        setMessage('Could not save the destination. Try again.')
        setSaving(false); savingRef.current = false
      }
      return
    }
    if (current !== generation.current) return
    setSaving(false); savingRef.current = false
    setMessage('Destination saved. Workspace and List selection is stored locally.')
    onChanged?.()
  }

  return <form className="settings-form clickup-destination" onSubmit={save}>
    <label>Workspace<select aria-label="Workspace" value={selection.workspace_id} disabled={busy || !workspaces.length} onChange={(event) => {
      const next = { workspace_id: event.target.value, space_id: '', list_id: '' }
      setSelection(next); setSpaces([]); setLists([]); load(1, next)
    }}>
      <option value="">Select Workspace</option>
      {workspaces.map((workspace) => <option key={workspace.id} value={id(workspace.id)}>{workspace.name}</option>)}
    </select></label>
    <label>Space<select aria-label="Space" value={selection.space_id} disabled={busy || !selection.workspace_id || !spaces.length} onChange={(event) => {
      const next = { ...selection, space_id: event.target.value, list_id: '' }
      setSelection(next); setLists([]); load(2, next)
    }}>
      <option value="">Select Space</option>
      {spaces.map((space) => <option key={space.id} value={id(space.id)}>{space.name}</option>)}
    </select></label>
    <label>List<select aria-label="List" value={selection.list_id} disabled={busy || !selection.space_id || !lists.length} onChange={(event) => {
      setSelection({ ...selection, list_id: event.target.value }); setMessage('')
    }}>
      <option value="">Select List</option>
      {lists.map((list) => <option key={list.id} value={id(list.id)}>{list.folder_name ? `${list.folder_name} / ${list.name}` : list.name}</option>)}
    </select></label>
    {loading && <p role="status">Loading ClickUp destinations…</p>}
    {!loading && !error && !workspaces.length && <p>No Workspaces available. Reconnect ClickUp to grant access.</p>}
    {!loading && !error && selection.workspace_id && !spaces.length && <p>No Spaces available in this Workspace.</p>}
    {!loading && !error && selection.space_id && !lists.length && <p>No Lists available in this Space.</p>}
    {error && <p role="alert">{error}</p>}
    <div className="integration-actions">
      <button type="submit" className="settings-primary" disabled={busy || !valid || !!error}>{saving ? 'Saving…' : 'Save destination'}</button>
      {error && <button type="button" disabled={busy} onClick={() => load(0, selection)}>Retry</button>}
    </div>
    {message && <p role="status">{message}</p>}
  </form>
}
