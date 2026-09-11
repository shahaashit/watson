import { useState } from 'react'
import { api } from '../api.js'
import { navigate } from '../routing.js'
import useDismissibleLayer from '../useDismissibleLayer.js'
import { syncMonitor } from '../useSyncRefresh.js'

// Adding work is local only: new cards belong to you, and importing a link
// copies the external work into Watson without writing back to it.
export default function AddWorkDialog({ onClose }) {
  const [mode, setMode] = useState('local')
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const ref = useDismissibleLayer({ open: true, onDismiss: () => { if (!busy) onClose() } })

  const value = mode === 'local' ? title.trim() : url.trim()
  const submit = async (event) => {
    event.preventDefault()
    if (!value || busy) return
    setBusy(true); setError('')
    const release = syncMonitor.hold()
    try {
      if (mode === 'local') {
        await api.createWork({ title: value, description: description.trim(), state: 'next' })
        release()
        syncMonitor.refresh()
        onClose()
        return
      }
      const response = await api.importWorkUrl(value)
      release()
      onClose()
      navigate(`/work/${response.work_item.id}`)
      return
    } catch {
      release()
      setError(mode === 'local' ? 'Could not add local work. Please try again.' : 'Could not import this link. Check the URL and integration in Settings, then try again.')
    } finally { setBusy(false) }
  }

  return <div className="modal-backdrop">
    <div className="add-work-dialog" ref={ref} role="dialog" aria-modal="true" aria-labelledby="add-work-title">
      <header><h2 id="add-work-title">Add work</h2><button type="button" onClick={onClose} aria-label="Close add work">×</button></header>
      <div className="add-work-modes" role="group" aria-label="Add work mode">
        <button type="button" className={mode === 'local' ? 'active' : ''} onClick={() => setMode('local')} aria-pressed={mode === 'local'}>Local work</button>
        <button type="button" className={mode === 'import' ? 'active' : ''} onClick={() => setMode('import')} aria-pressed={mode === 'import'}>Import link</button>
      </div>
      <form onSubmit={submit}>
        {mode === 'local' ? <>
          <label htmlFor="add-work-title-input">Title</label>
          <input id="add-work-title-input" autoFocus value={title} onChange={(event) => setTitle(event.target.value)} placeholder="What needs your attention?" />
          <label htmlFor="add-work-description">Description <span>optional</span></label>
          <textarea id="add-work-description" rows="4" value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Anything you want to remember about it…" />
          <p className="add-work-note">Added to your lane on this board.</p>
        </> : <>
          <label htmlFor="add-work-url">GitLab MR or ClickUp task URL</label>
          <input id="add-work-url" type="url" autoFocus value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://gitlab…/merge_requests/42 or https://app.clickup.com/…" />
          <p className="add-work-note">Imports the linked work into Watson; it does not change ClickUp or GitLab.</p>
        </>}
        {error && <p className="work-inline-error" role="alert">{error}</p>}
        <div className="add-work-dialog-actions">
          <button type="button" className="quiet" disabled={busy} onClick={onClose}>Cancel</button>
          <button type="submit" className="primary" disabled={busy || !value}>{busy ? (mode === 'local' ? 'Adding…' : 'Importing…') : (mode === 'local' ? 'Add work' : 'Import')}</button>
        </div>
      </form>
    </div>
  </div>
}
