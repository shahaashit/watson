import { useState } from 'react'
import { api } from '../api.js'

function plusOneHour() {
  return new Date(Date.now() + 3600_000).toISOString().slice(0, 19)
}

export default function WorkFollowups({ reminders = [], onChanged }) {
  const [busyId, setBusyId] = useState(null)
  const [error, setError] = useState('')
  const run = async (id, action) => {
    setBusyId(id); setError('')
    try { await action(); await onChanged?.() }
    catch { setError('Could not update this follow-up. Please try again.') }
    finally { setBusyId(null) }
  }

  return <section className="work-followups" aria-labelledby="followups-title">
    <div className="work-section-heading"><h2 id="followups-title">Follow-ups</h2><span>{reminders.length}</span></div>
    {error && <p className="work-inline-error" role="alert">{error}</p>}
    {!reminders.length ? <p className="work-detail-empty">No reminders or follow-ups attached.</p> : <ul className="followup-list">
      {reminders.map((reminder) => <li key={reminder.id} className={reminder.status === 'done' ? 'is-done' : ''}>
        <div><strong>{reminder.text}</strong><time dateTime={reminder.due_at}>Due {reminder.due_at?.replace('T', ' ').slice(0, 16) || 'unscheduled'}</time></div>
        {reminder.status !== 'done' && <div className="followup-actions">
          <button type="button" disabled={busyId === reminder.id} onClick={() => run(reminder.id, () => api.reminderDone(reminder.id))}>Done</button>
          <button type="button" disabled={busyId === reminder.id} onClick={() => run(reminder.id, () => api.reminderSnooze(reminder.id, plusOneHour()))}>+1h</button>
        </div>}
      </li>)}
    </ul>}
  </section>
}
