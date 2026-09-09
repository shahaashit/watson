import { useState } from 'react'
import { api } from '../api.js'

function tomorrowAt10() {
  const d = new Date(Date.now() + 24 * 3600_000)
  d.setHours(10, 0, 0, 0)
  return localIso(d)
}

function localIso(d) {
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:00`
}

export default function ReminderItem({ reminder, onChanged }) {
  const [busy, setBusy] = useState(false)
  const overdue = reminder.status !== 'done' && reminder.due_at < localIso(new Date())

  const run = async (fn) => {
    setBusy(true)
    try {
      await fn()
      onChanged?.()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={`reminder ${reminder.status} ${overdue ? 'overdue' : ''}`}>
      <span className="reminder-text">{reminder.text}</span>
      <span className="reminder-due">{reminder.due_at?.slice(0, 16).replace('T', ' ')}</span>
      {reminder.status !== 'done' && (
        <span className="reminder-buttons">
          <button className="btn small" disabled={busy} onClick={() => run(() => api.reminderDone(reminder.id))}>
            Done
          </button>
          <button
            className="btn small"
            disabled={busy}
            title="Snooze 1 hour"
            onClick={() => run(() => api.reminderSnooze(reminder.id, localIso(new Date(Date.now() + 3600_000))))}
          >
            +1h
          </button>
          <button
            className="btn small"
            disabled={busy}
            title="Snooze to tomorrow 10:00"
            onClick={() => run(() => api.reminderSnooze(reminder.id, tomorrowAt10()))}
          >
            Tmrw
          </button>
        </span>
      )}
    </div>
  )
}
