import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { navigate } from '../routing.js'
import ActivityThread from '../components/ActivityThread.jsx'
import LinkedWork from '../components/LinkedWork.jsx'
import PendingAction from '../components/PendingAction.jsx'
import WorkFollowups from '../components/WorkFollowups.jsx'

const ACTIVITY_TYPES = [
  ['note', 'Note'],
  ['decision', 'Decision'],
  ['blocker', 'Blocker'],
]
function readableTime(value) {
  if (!value) return 'Not available'
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString(undefined, { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' })
}

function ownerLabel(item, people) {
  if (item.owner_display) return item.owner_display
  const person = people.find((entry) => entry.id === item.owner_person_id)
  if (person?.display_name) return person.display_name
  return item.owner_person_id ? 'Configured owner' : 'Unassigned'
}

function CaptureToWork({ workItemId, onCaptured }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const submit = async (event) => {
    event.preventDefault()
    const value = text.trim()
    if (!value || busy) return
    setBusy(true); setError(''); setMessage('')
    try {
      await api.capture(value, workItemId)
      setText('')
      setMessage('Captured to this work.')
      await onCaptured?.()
    } catch { setError('Could not confirm this note was attached here. It may still be in Watson captures; refresh and try again.') }
    finally { setBusy(false) }
  }
  return <section className="capture-to-work" aria-labelledby="capture-to-work-title">
    <div className="work-section-heading"><h2 id="capture-to-work-title">Capture to this work</h2></div>
    <p>Keep a note or update alongside this work. Your text is saved first, then organized by Watson.</p>
    <form onSubmit={submit}><label className="sr-only" htmlFor="work-capture-text">Capture for this work</label><textarea id="work-capture-text" value={text} onChange={(event) => setText(event.target.value)} placeholder="Capture a note, update, or context for this work…" rows="3" disabled={busy} /><button type="submit" disabled={busy || !text.trim()}>{busy ? 'Capturing…' : 'Capture'}</button></form>
    {message && <p className="work-capture-success" role="status">{message}</p>}
    {error && <p className="work-inline-error" role="alert">{error}</p>}
  </section>
}

export default function WorkDetail({ workItemId }) {
  const [detail, setDetail] = useState(null)
  const [people, setPeople] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [activityType, setActivityType] = useState('note')
  const [activityBody, setActivityBody] = useState('')
  const [activityBusy, setActivityBusy] = useState(false)
  const [activityError, setActivityError] = useState('')
  const sourceBoard = window.history.state?.sourceBoard === 'team' ? 'team' : 'my-work'

  const loadDetail = useCallback(async (signal) => {
    try {
      const response = await api.workDetail(workItemId, { signal })
      setDetail(response.work_item); setError('')
    } catch (err) {
      if (err.name !== 'AbortError') setError('This work item could not be loaded. It may have been removed.')
    } finally { if (!signal?.aborted) setLoading(false) }
  }, [workItemId])

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true); setDetail(null)
    loadDetail(controller.signal)
    api.people({ signal: controller.signal }).then((response) => setPeople(response.people || [])).catch(() => setPeople([]))
    return () => controller.abort()
  }, [loadDetail])

  const submitActivity = async (event) => {
    event.preventDefault()
    const body = activityBody.trim()
    if (!body || activityBusy) return
    setActivityBusy(true); setActivityError('')
    try {
      await api.addWorkActivity(workItemId, { activity_type: activityType, body })
      setActivityBody('')
      await loadDetail()
    } catch { setActivityError('Could not add activity. Please try again.') }
    finally { setActivityBusy(false) }
  }
  if (loading) return <p className="work-loading">Loading work context…</p>
  if (error && !detail) return <div className="work-detail-load-error" role="alert"><p>{error}</p><button type="button" onClick={() => { setLoading(true); loadDetail() }}>Try again</button><button type="button" onClick={() => navigate(`/${sourceBoard}`)}>Back to board</button></div>
  if (!detail) return null
  const owner = ownerLabel(detail, people)

  return <div className="work-detail-page">
    <button type="button" className="back-to-board" onClick={() => navigate(`/${sourceBoard}`)}>← Back to {sourceBoard === 'team' ? 'Team' : 'My Work'}</button>
    {error && <p className="work-inline-error" role="alert">{error}</p>}
    <header className="work-detail-header"><div><p className="eyebrow">Work details</p><h1>{detail.title}</h1>{detail.description && <p className="work-detail-description">{detail.description}</p>}</div></header>
    <dl className="work-metadata"><div><dt>Owner</dt><dd>{owner}</dd></div><div><dt>Created</dt><dd>{readableTime(detail.created_at)}</dd></div><div><dt>Updated</dt><dd>{readableTime(detail.updated_at)}</dd></div>{detail.completed_at && <div><dt>Completed</dt><dd>{readableTime(detail.completed_at)}</dd></div>}</dl>
    <div className="work-detail-grid"><div className="work-detail-main">
      <CaptureToWork workItemId={detail.id} onCaptured={() => loadDetail()} />
      <section className="activity-composer" aria-labelledby="add-activity-title"><div className="work-section-heading"><h2 id="add-activity-title">Add activity</h2></div><form onSubmit={submitActivity}><label className="sr-only" htmlFor="activity-body">Activity details</label><textarea id="activity-body" value={activityBody} onChange={(event) => setActivityBody(event.target.value)} placeholder="Note a decision, blocker, or update…" rows="3" disabled={activityBusy} /><div><label htmlFor="activity-type">Type</label><select id="activity-type" value={activityType} onChange={(event) => setActivityType(event.target.value)} disabled={activityBusy}>{ACTIVITY_TYPES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><button type="submit" disabled={activityBusy || !activityBody.trim()}>{activityBusy ? 'Saving…' : 'Add activity'}</button></div></form>{activityError && <p className="work-inline-error" role="alert">{activityError}</p>}</section>
      <ActivityThread activity={detail.activity} />
    </div><aside className="work-detail-support">
      <LinkedWork links={detail.links} />
      <WorkFollowups reminders={detail.reminders} onChanged={() => loadDetail()} />
      <section className="involved-people" aria-labelledby="people-title"><div className="work-section-heading"><h2 id="people-title">Involved people</h2></div><p>{owner === 'Unassigned' ? 'No owner has been resolved yet.' : owner}</p></section>
      <section className="pending-work-actions" aria-labelledby="pending-actions-title"><div className="work-section-heading"><h2 id="pending-actions-title">Pending actions</h2><span>{detail.pending_actions?.length || 0}</span></div>{!detail.pending_actions?.length ? <p className="work-detail-empty">No external writes are awaiting approval.</p> : detail.pending_actions.map((action) => <PendingAction key={action.id} action={action} onChanged={() => loadDetail()} />)}</section>
    </aside></div>
  </div>
}
