// Right sidebar — your "today" snapshot, visible on every tab.
// All data is DB-only (no live API calls), so polling is cheap.
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import ReminderItem from './ReminderItem.jsx'

export default function TodayPanel({ onChanged }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  const refresh = useCallback(() => {
    api.today().then(setData).catch((e) => setError(e.message))
  }, [])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 60_000)
    return () => clearInterval(id)
  }, [refresh])

  // expose a refresh hook for action handlers elsewhere
  useEffect(() => {
    const onTodayDirty = () => refresh()
    window.addEventListener('watson:today-dirty', onTodayDirty)
    return () => window.removeEventListener('watson:today-dirty', onTodayDirty)
  }, [refresh])

  if (!data && !error) {
    return <aside className="today-panel"><p className="empty">Loading…</p></aside>
  }
  if (error) {
    return <aside className="today-panel"><p className="error-text">{error}</p></aside>
  }

  const meetings = data.meetings || []
  const reminders = data.reminders_due || []
  const stale = data.stale_mrs || []
  const pending = data.pending_count || 0
  const todayLabel = new Date().toLocaleDateString(undefined,
    { weekday: 'long', day: 'numeric', month: 'short' })

  const reminderChanged = () => {
    refresh()
    onChanged?.()
  }

  return (
    <aside className="today-panel">
      <header className="today-panel-head">
        <h3 className="today-panel-title">Today</h3>
        <span className="today-panel-date">{todayLabel}</span>
      </header>

      <section className="today-panel-section">
        <div className="today-panel-label">
          Meetings <span className="count">{meetings.length}</span>
        </div>
        {meetings.length > 0 ? (
          meetings.map((m) => (
            <MeetingRow key={m.event_id} meeting={m} />
          ))
        ) : (
          <p className="today-panel-empty">No meetings today.</p>
        )}
      </section>


      <section className="today-panel-section">
        <div className="today-panel-label">
          Reminders due <span className="count">{reminders.length}</span>
        </div>
        {reminders.length > 0 ? (
          reminders.map((r) => (
            <ReminderItem key={r.id} reminder={r} onChanged={reminderChanged} />
          ))
        ) : (
          <p className="today-panel-empty">Nothing due, clear runway.</p>
        )}
      </section>

      <section className="today-panel-section">
        <div className="today-panel-counters">
          <div className={`counter ${pending > 0 ? 'attention' : ''}`}>
            <span className="counter-num">{pending}</span>
            <span className="counter-label">waiting on you</span>
          </div>
          <div className={`counter ${stale.length > 0 ? 'attention' : ''}`}>
            <span className="counter-num">{stale.length}</span>
            <span className="counter-label">stale reviews</span>
          </div>
        </div>
      </section>
    </aside>
  )
}


function MeetingRow({ meeting }) {
  const when = meeting.all_day
    ? 'All-day'
    : formatTime(meeting.start_at)
  const inner = (
    <>
      <span className="today-panel-meeting-when">{when}</span>
      <span className="today-panel-meeting-title">{meeting.title || '(no title)'}</span>
    </>
  )
  return meeting.html_link ? (
    <a className="today-panel-meeting" href={meeting.html_link} target="_blank" rel="noreferrer">
      {inner}
    </a>
  ) : (
    <div className="today-panel-meeting">{inner}</div>
  )
}


function formatTime(iso) {
  // start_at is an RFC3339 string with the user's local TZ offset; slice the
  // HH:MM out of the T portion. Avoids Date() parsing quirks around DST.
  if (!iso || iso.length < 16) return iso || ''
  return iso.slice(11, 16)
}


function FlockRow({ chat }) {
  const prefix = chat.is_group ? '#' : '@'
  const label = chat.name || '(no name)'
  const mentions = chat.mentions || []
  const meta = chat.has_mention
    ? `${chat.unread_count} unread · mention`
    : mentions.length
      ? `${mentions.length} @-mention${mentions.length === 1 ? '' : 's'}`
      : `${chat.unread_count} unread`
  // Show up to 2 most recent mentions as previews so the user can decide
  // at-a-glance whether the chat needs their attention now.
  const previews = mentions.slice(0, 2)
  return (
    <a className="today-panel-flock" href={chat.url} target="_blank" rel="noreferrer">
      <div className="today-panel-flock-head">
        <span className="today-panel-flock-name">{prefix}{label}</span>
        <span className="today-panel-flock-meta">{meta}</span>
      </div>
      {previews.length > 0 && (
        <div className="today-panel-flock-previews">
          {previews.map((m) => (
            <MentionPreview key={m.id} mention={m} />
          ))}
        </div>
      )}
    </a>
  )
}


function MentionPreview({ mention }) {
  // Bot heuristic: sender_name gets set to sender_jid when we couldn't
  // resolve the JID against the sidebar buddy list. That's almost always a
  // bot / alert sender — showing the JID is noise, so hide the byline and
  // just lead with the message text. Human buddies get their real name.
  const senderRaw = mention.sender_name || mention.sender || ''
  const isBot = !senderRaw || senderRaw === mention.sender
  const text = cleanMentionText(mention.text)
  return (
    <div className={`today-panel-flock-preview${isBot ? ' is-bot' : ''}`}>
      {!isBot && (
        <div className="today-panel-flock-sender">{senderRaw}</div>
      )}
      <div className="today-panel-flock-text">{text || '(no text)'}</div>
    </div>
  )
}


function cleanMentionText(t) {
  if (!t) return ''
  // Strip the leading @-you fragment — the chat card already tells the
  // user they were @-mentioned; repeating "@Taylor S " on every preview is
  // just visual noise that eats into the truncated character budget.
  return t.replace(/^@[^\s]+(?:\s[^\s]+)?\s+/, '').trim()
}
