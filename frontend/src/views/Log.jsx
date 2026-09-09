// Unified activity log — merges captured entries with Watson's own actions
// (sync ticks, drafts, approvals, rejections) from /api/log.
import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import EntryCard from '../components/EntryCard.jsx'
import { navigate } from '../routing.js'
import { useCallback } from 'react'
import { useCacheRefresh } from '../useSyncRefresh.js'

// Icon per row kind. Entries use their `type`; events use the event `kind`.
const ICON = {
  // entry types
  discussion: '💬', meeting: '📅', task: '✅', note: '📝', status_update: '📣',
  // event kinds
  sync: '⏱', proposal: '✨', approval: '✔', rejection: '✕', backfill: '📥',
  // local work kinds
  work_activity: '✦', work_completed: '✓',
}

const KIND_LABEL = {
  sync: 'Sync', proposal: 'Proposal', approval: 'Approved', rejection: 'Rejected',
  backfill: 'Backfill',
  discussion: 'Discussion', meeting: 'Meeting', task: 'Task', note: 'Note',
  status_update: 'Status update',
  work_activity: 'Work activity', work_completed: 'Completed work',
}

const SOURCE_FILTERS = [
  { key: null,      label: 'All' },
  { key: 'entry',   label: 'My notes' },
  { key: 'event',   label: 'Watson activity' },
  { key: 'work',    label: 'Work context' },
]

export default function Log({ initialQuery = '' }) {
  const [query, setQuery] = useState(initialQuery)
  useEffect(() => { if (initialQuery) setQuery(initialQuery) }, [initialQuery])

  const [source, setSource] = useState(null)
  const [kind, setKind] = useState(null)
  const [items, setItems] = useState([])
  const [kinds, setKinds] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [reload, setReload] = useState(0)
  const logGenerationRef = useRef(0)
  const refresh = useCallback(() => setReload(n => n + 1), [])
  useCacheRefresh(refresh)

  useEffect(() => {
    const generation = ++logGenerationRef.current
    const params = {}
    if (query) params.q = query
    if (source) params.source = source
    if (kind) params.kind = kind
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    const id = setTimeout(() => {
      api.log(params, { signal: controller.signal })
        .then((r) => {
          if (generation !== logGenerationRef.current) return
          setItems(r.items)
          setKinds(r.kinds || [])
          setError(null)
          setLoading(false)
        })
        .catch((e) => {
          if (e.name !== 'AbortError' && generation === logGenerationRef.current) {
            setError(e.message)
            setLoading(false)
          }
        })
    }, 200)
    return () => {
      clearTimeout(id)
      controller.abort()
      if (logGenerationRef.current === generation) logGenerationRef.current += 1
    }
  }, [query, source, kind, reload])

  const handleDelete = async (entry) => {
    try {
      await api.deleteEntry(entry.entry_id ?? entry.id)
      setReload((n) => n + 1)
    } catch (e) { setError(e.message) }
  }

  return (
    <div className="log-view">
      <header className="settings-page-header"><p className="eyebrow">Log</p><h1>Your work, in perspective.</h1><p>Find your notes, decisions and updates in one timeline.</p></header>
      <input
        className="search"
        placeholder="Search notes and activity…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        autoFocus
      />
      <div className="filter-row">
        {SOURCE_FILTERS.map((f) => (
          <button key={f.label}
            className={`chip filter ${source === f.key ? 'active' : ''}`}
            onClick={() => { setSource(f.key); setKind(null) }}>
            {f.label}
          </button>
        ))}
      </div>
      {kinds.length > 0 && (
        <div className="filter-row tags">
          {kinds.map((k) => (
            <button key={k}
              className={`chip tag ${kind === k ? 'active' : ''}`}
              onClick={() => setKind(kind === k ? null : k)}>
              {ICON[k] || '•'} {KIND_LABEL[k] || k}
            </button>
          ))}
        </div>
      )}
      {error && <p className="error-text">{error}</p>}
      <div className="entry-list">
        {loading && !items.length && <p className="empty">Loading timeline…</p>}
        {items.map((item) => (
          item.source === 'entry'
            ? <EntryCard key={item.id} entry={{ ...item, id: item.entry_id }}
                        onDelete={handleDelete} />
            : item.source === 'work'
              ? <WorkRow key={item.id} item={item} />
              : <EventRow key={item.id} event={item} />
        ))}
        {!loading && items.length === 0 && <p className="empty">Nothing to show.</p>}
      </div>
    </div>
  )
}

function WorkRow({ item }) {
  const time = (item.created_at || '').replace('T', ' ').slice(0, 16)
  const activityLabel = item.kind === 'work_completed'
    ? 'Completed'
    : (item.activity_type || 'activity').replace('_', ' ')
  return (
    <button type="button" className={`event-row work-log-row kind-${item.kind}`}
      onClick={() => navigate(item.url)} aria-label={`Open work: ${item.title}`}>
      <span className="event-icon">{ICON[item.kind] || '•'}</span>
      <span className="event-body">
        <span className="event-title">{item.title}</span>
        {item.body && <span className="work-log-body">{item.body}</span>}
        <span className="event-meta">
          <span className="event-kind">{activityLabel}</span>
          <span className="event-time">{time}</span>
        </span>
      </span>
    </button>
  )
}

function EventRow({ event }) {
  const time = (event.created_at || '').replace('T', ' ').slice(0, 16)
  return (
    <div className={`event-row kind-${event.kind}`}>
      <span className="event-icon">{ICON[event.kind] || '•'}</span>
      <div className="event-body">
        <div className="event-title">{event.title}</div>
        <div className="event-meta">
          <span className="event-kind">{KIND_LABEL[event.kind] || event.kind}</span>
          <span className="event-time">{time}</span>
        </div>
      </div>
    </div>
  )
}
