// ⌘K command bar — universal search + quick actions, open from anywhere.
import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api.js'

const KIND_LABEL = {
  entry: 'Entry', capture: 'Capture',
  work_item: 'Work', work_activity: 'Work activity',
  clickup_task: 'ClickUp', mr: 'MR',
}

function buildActions(setView, syncAll, addWork, navigateTo) {
  return [
    { id: 'a-my-work', icon: '📋', title: 'My Work',
      subtitle: 'Your prioritized work', keywords: 'today home work', run: () => setView('my-work') },
    { id: 'a-team', icon: '👥', title: 'Team',
      subtitle: 'Tracked people and their work', keywords: 'team people lanes', run: () => setView('team') },
    { id: 'a-add-work', icon: '＋', title: 'Add Work',
      subtitle: 'Create a local work item', keywords: 'create new work task', run: () => addWork() },
    { id: 'a-capture', icon: '📝', title: 'Capture',
      subtitle: 'Focus the capture box', keywords: 'capture note new',
      run: () => { setView('my-work'); window.setTimeout(() => window.dispatchEvent(new Event('watson:focus-capture')), 0) } },
    { id: 'a-ask', icon: '🔍', title: 'Ask Watson',
      subtitle: 'Question your log', keywords: 'ask query search question',
      run: () => { setView('my-work'); window.setTimeout(() => window.dispatchEvent(new Event('watson:focus-ask')), 0) } },
    { id: 'a-sync', icon: '⏱', title: 'Sync now',
      subtitle: 'Refresh ClickUp + GitLab + Calendar + Flock', keywords: 'sync refresh',
      run: () => { syncAll() } },
    { id: 'a-settings', icon: '⚙', title: 'Settings',
      subtitle: 'Connections, people, sync, and backup', keywords: 'settings integration people backup',
      run: () => navigateTo('/settings') },
    { id: 'a-log', icon: '📚', title: 'Go to Log',
      subtitle: 'Search timeline', keywords: 'log history activity', run: () => setView('log') },
  ]
}

export default function CommandBar({ setView, syncAll, setLogQuery, addWork, navigateTo }) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [results, setResults] = useState([])
  const [selected, setSelected] = useState(0)
  const inputRef = useRef(null)
  const searchGenerationRef = useRef(0)

  // ⌘K toggles; Esc closes
  useEffect(() => {
    const onKey = (e) => {
      const meta = e.metaKey || e.ctrlKey
      if (meta && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault()
        setOpen((o) => !o)
      } else if (e.key === 'Escape' && open) {
        setOpen(false)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  // reset on open
  useEffect(() => {
    if (open) {
      setQuery('')
      setResults([])
      setSelected(0)
      setTimeout(() => inputRef.current?.focus(), 30)
    }
  }, [open])

  // search as you type (debounced)
  useEffect(() => {
    const generation = ++searchGenerationRef.current
    if (!open) return undefined
    const q = query.trim()
    if (!q) { setResults([]); return }
    const controller = new AbortController()
    const id = setTimeout(() => {
      api.search(q, { signal: controller.signal })
        .then((r) => {
          if (generation === searchGenerationRef.current) setResults(r.results || [])
        })
        .catch((error) => {
          if (error.name !== 'AbortError' && generation === searchGenerationRef.current) setResults([])
        })
    }, 130)
    return () => {
      clearTimeout(id)
      controller.abort()
      if (searchGenerationRef.current === generation) searchGenerationRef.current += 1
    }
  }, [query, open])

  const actions = useMemo(
    () => buildActions(setView, syncAll, addWork, navigateTo),
    [setView, syncAll, addWork, navigateTo],
  )
  const q = query.trim().toLowerCase()
  const filteredActions = useMemo(() => actions.filter((a) =>
    !q || (a.title + ' ' + (a.subtitle || '') + ' ' + a.keywords).toLowerCase().includes(q)
  ), [actions, q])

  // ordered list: actions first, then by-kind result groups
  const combined = useMemo(() => {
    const items = [...filteredActions.map((a) => ({ ...a, _action: true }))]
    for (const kind of ['work_item', 'work_activity', 'entry', 'capture', 'clickup_task', 'mr']) {
      for (const r of results.filter((x) => x.kind === kind)) items.push(r)
    }
    return items
  }, [filteredActions, results])

  useEffect(() => {
    if (combined.length === 0) { setSelected(0); return }
    setSelected((current) => Math.max(0, Math.min(current, combined.length - 1)))
  }, [combined])

  const activate = (item) => {
    if (item._action) { item.run(); setOpen(false); return }
    if (item.kind === 'work_item' || item.kind === 'work_activity') {
      navigateTo(item.url); setOpen(false); return
    }
    if (item.url) { window.open(item.url, '_blank', 'noopener,noreferrer'); setOpen(false); return }
    // internal jumps
    if (item.kind === 'entry' || item.kind === 'capture') {
      setLogQuery?.(query)
      setView('log')
      setOpen(false)
    }
  }

  const onInputKey = (e) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      if (combined.length === 0) return
      setSelected((s) => Math.max(0, Math.min(s + 1, combined.length - 1)))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      if (combined.length === 0) return
      setSelected((s) => Math.max(s - 1, 0))
    } else if (e.key === 'Enter') {
      const item = combined[selected]
      if (item) { e.preventDefault(); activate(item) }
    }
  }

  if (!open) return null

  return (
    <div className="cmdk-overlay" onClick={() => setOpen(false)}>
      <div className="cmdk-modal" onClick={(e) => e.stopPropagation()}>
        <div className="cmdk-input-row">
          <span className="cmdk-input-icon">⌘</span>
          <input
            ref={inputRef}
            className="cmdk-input"
            placeholder="Search captures, entries, work, tasks, MRs… or run a command"
            value={query}
            onChange={(e) => { setQuery(e.target.value); setSelected(0) }}
            onKeyDown={onInputKey}
          />
        </div>
        <div className="cmdk-results">
          {combined.length === 0 ? (
            <div className="cmdk-empty">
              {q ? 'No matches' : 'Start typing to search, or run a command'}
            </div>
          ) : combined.map((item, i) => (
            <button key={`${item._action ? 'act' : item.kind}-${item.id}-${i}`}
                    className={`cmdk-row ${i === selected ? 'selected' : ''}`}
                    onClick={() => activate(item)}
                    onMouseEnter={() => setSelected(i)}>
              <span className="cmdk-icon">{item.icon}</span>
              <span className="cmdk-main">
                <span className="cmdk-title">{item.title}</span>
                {item.subtitle && <span className="cmdk-sub">{item.subtitle}</span>}
              </span>
              {!item._action && (
                <span className="cmdk-kind">{KIND_LABEL[item.kind] || item.kind}</span>
              )}
            </button>
          ))}
        </div>
        <div className="cmdk-foot">
          <span><kbd>↑</kbd><kbd>↓</kbd> navigate</span>
          <span><kbd>↵</kbd> select</span>
          <span><kbd>esc</kbd> close</span>
        </div>
      </div>
    </div>
  )
}
