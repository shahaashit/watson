import { useState } from 'react'

const TYPE_ICONS = {
  discussion: '💬',
  meeting: '📅',
  task: '✅',
  note: '📝',
  status_update: '📣',
}

export default function EntryCard({ entry, onTagClick, onDelete }) {
  const [expanded, setExpanded] = useState(false)
  const followUps = entry.follow_ups || []

  const handleDelete = (e) => {
    e.stopPropagation()
    const n = followUps.length
    const extra = n > 0 ? ` and its ${n} follow-up${n > 1 ? 's' : ''}` : ''
    if (window.confirm(`Delete "${entry.title}"${extra}? This can't be undone.`)) {
      onDelete(entry)
    }
  }

  return (
    <div className="entry-card" onClick={() => setExpanded(!expanded)}>
      <div className="entry-head">
        <span className="entry-type" title={entry.type}>{TYPE_ICONS[entry.type] || '📄'}</span>
        <span className="entry-title">
          {entry.parent_entry_id && <span className="followup-marker" title="follow-up">↳ </span>}
          {entry.title}
        </span>
        {followUps.length > 0 && (
          <span className="thread-count" title="follow-ups in this thread">
            {followUps.length} update{followUps.length > 1 ? 's' : ''}
          </span>
        )}
        <span className="entry-date">{entry.created_at?.slice(0, 16).replace('T', ' ')}</span>
        {onDelete && (
          <button className="entry-delete" title="Delete entry" onClick={handleDelete}>✕</button>
        )}
      </div>
      {(entry.people.length > 0 || entry.tags.length > 0) && (
        <div className="entry-meta">
          {entry.people.map((p) => (
            <span key={p} className="chip person">{p}</span>
          ))}
          {entry.tags.map((t) => (
            <span
              key={t}
              className="chip tag"
              onClick={(e) => { e.stopPropagation(); onTagClick?.(t) }}
            >
              #{t}
            </span>
          ))}
        </div>
      )}
      {expanded && (
        <div className="entry-detail">
          {entry.body && <p className="entry-body">{entry.body}</p>}
          {entry.capture_raw && (
            <p className="entry-raw">
              <span className="raw-label">raw capture:</span> {entry.capture_raw}
            </p>
          )}
        </div>
      )}
      {followUps.length > 0 && (
        <div className="thread" onClick={(e) => e.stopPropagation()}>
          {followUps.map((f) => (
            <div key={f.id} className="thread-item">
              <span className="thread-date">{f.created_at?.slice(0, 16).replace('T', ' ')}</span>
              <span className="thread-title">{f.title}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
