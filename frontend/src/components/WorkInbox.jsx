import { useState } from 'react'
import { api } from '../api.js'

export default function WorkInbox({ items = [], work = {}, onChanged }) {
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState('')
  const [selectedWork, setSelectedWork] = useState({})

  const resolve = async (item, workItemId) => {
    if (!workItemId) return
    setBusy(item.id); setError('')
    try {
      await api.resolveInbox(item.id, Number(workItemId))
      onChanged?.(item.id)
    } catch (err) { setError(`Could not link capture. ${err.message}`) } finally { setBusy(null) }
  }
  const dismiss = async (item) => {
    setBusy(item.id); setError('')
    try {
      await api.dismissInbox(item.id)
      onChanged?.(item.id)
    } catch (err) { setError(`Could not dismiss capture. ${err.message}`) } finally { setBusy(null) }
  }
  const options = work.items?.length
    ? work.items
    : ['today', 'next', 'waiting', 'done'].flatMap((state) => work[state] || [])

  return (
    <section className="work-inbox" aria-label="Inbox">
      <div className="work-list-heading"><h2>Inbox</h2><span>{items.length}</span></div>
      {error && <p className="work-inline-error" role="alert">{error}</p>}
      {!items.length ? <p className="work-list-empty">Captured thoughts without a work link appear here.</p> : items.map((item) => (
        <article key={item.id} className="inbox-card">
          <p>{item.capture_text}</p>
          {item.suggested_work_item && <p className="inbox-suggestion">Suggested: {item.suggested_work_item.title}</p>}
          <div className="inbox-actions">
            <label className="sr-only" htmlFor={`inbox-work-${item.id}`}>Choose work for capture</label>
            <select id={`inbox-work-${item.id}`} value={selectedWork[item.id] ?? item.suggested_work_item?.id ?? ''} onChange={(event) => setSelectedWork((current) => ({ ...current, [item.id]: event.target.value }))} disabled={busy === item.id}>
              <option value="">Choose work…</option>
              {options.map((workItem) => <option key={workItem.id} value={workItem.id}>{workItem.title}</option>)}
            </select>
            <button type="button" disabled={busy === item.id || !(selectedWork[item.id] ?? item.suggested_work_item?.id)} onClick={() => resolve(item, selectedWork[item.id] ?? item.suggested_work_item?.id)}>Link</button>
            <button type="button" className="quiet" disabled={busy === item.id} onClick={() => dismiss(item)}>Dismiss</button>
          </div>
        </article>
      ))}
    </section>
  )
}
