import { useState } from 'react'
import { api } from '../api.js'
import PendingAction from './PendingAction.jsx'

function ReviewDecision({ item, onChanged }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const payload = item.payload || {}
  const members = payload.member_mr_ids || []
  const run = async (operation) => {
    if (busy) return
    setBusy(true); setError('')
    try { await operation(); onChanged?.() }
    catch (err) { setError(err.message) }
    finally { setBusy(false) }
  }

  if (item.kind === 'group_review_mrs') return <article className="watson-suggestion-card">
    <div className="suggestion-kicker">Grouping decision</div>
    <strong>{payload.suggested_title || 'Related review MRs'}</strong>
    <p>{payload.reasoning || 'These merge requests may belong to one review task.'}</p>
    <div className="suggestion-mrs">{members.map((mrId) => <span key={mrId}>{mrId}</span>)}</div>
    <div className="action-buttons">
      <button className="btn approve" disabled={busy} onClick={() => run(() => api.approveAction(item.id))}>Merge</button>
      <button className="btn reject" disabled={busy} onClick={() => run(() => api.rejectAction(item.id))}>Keep separate</button>
    </div>
    {error && <p className="work-inline-error" role="alert">{error}</p>}
  </article>

  const retryable = item.retryable && item.group_id != null && ['failed', 'deferred'].includes(item.status)
  return <article className={`watson-suggestion-card ${item.status}`}>
    <div className="suggestion-kicker">Review task {item.status}</div>
    <strong>{payload.suggested_title || payload.draft?.name || 'Review task'}</strong>
    {members.length > 0 && <div className="suggestion-mrs">{members.map((mrId) => <span key={mrId}>{mrId}</span>)}</div>}
    {item.status === 'uncertain'
      ? <p>Check ClickUp before resolving. Watson will not retry an uncertain external write.</p>
      : <p>{item.error || 'The ClickUp review task was not created.'}</p>}
    {retryable && <div className="action-buttons"><button className="btn approve" disabled={busy} onClick={() => run(() => api.retryReviewGroup(item.group_id))}>Retry</button></div>}
    {error && <p className="work-inline-error" role="alert">{error}</p>}
  </article>
}

export default function WatsonSuggestions({ items = [], onChanged }) {
  if (!items.length) return null
  return <section className="watson-suggestions" aria-label="Watson suggestions">
    <div className="work-list-heading"><h2>Watson suggests</h2><span>{items.length}</span></div>
    <div className="watson-suggestion-list">
      {items.map((item) => item.kind === 'clickup_close_task'
        ? <PendingAction key={item.id} action={item} onChanged={onChanged} />
        : <ReviewDecision key={item.id} item={item} onChanged={onChanged} />)}
    </div>
  </section>
}
