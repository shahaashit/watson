import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { navigate } from '../routing.js'
import Markdown from '../components/Markdown.jsx'
import ScheduleStrip from '../components/ScheduleStrip.jsx'
import PersonalTaskList from '../components/PersonalTaskList.jsx'
import { moveBoardCard } from '../boardOrder.js'
import { syncMonitor, useCacheRefresh } from '../useSyncRefresh.js'

const EMPTY_WORK = { items: [] }

function CompactCapture({ onCaptured }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState('')
  const [answer, setAnswer] = useState(null)
  const [error, setError] = useState('')
  const inputRef = useRef(null)

  useEffect(() => {
    const focus = () => inputRef.current?.focus()
    window.addEventListener('watson:focus-capture', focus)
    window.addEventListener('watson:focus-ask', focus)
    return () => { window.removeEventListener('watson:focus-capture', focus); window.removeEventListener('watson:focus-ask', focus) }
  }, [])

  const submit = async (kind) => {
    const value = text.trim()
    if (!value || busy) return
    setBusy(kind); setError(''); setAnswer(null)
    try {
      if (kind === 'capture') {
        await api.capture(value)
        setText('')
        onCaptured?.()
      } else {
        setAnswer(await api.ask(value))
      }
    } catch (err) { setError(err.message) } finally { setBusy('') }
  }
  const keyDown = (event) => {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault(); submit(event.shiftKey ? 'ask' : 'capture')
    }
  }

  return <section className="compact-capture" aria-label="Capture or ask Watson">
    {answer && <div className="compact-answer"><div><span>Watson says</span><button type="button" onClick={() => setAnswer(null)} aria-label="Dismiss answer">×</button></div><Markdown text={answer.answer} /></div>}
    <div className="compact-capture-row">
      <input ref={inputRef} value={text} onChange={(event) => setText(event.target.value)} onKeyDown={keyDown} disabled={Boolean(busy)} placeholder="Capture a thought or ask Watson…" />
      <button type="button" disabled={!text.trim() || Boolean(busy)} onClick={() => submit('ask')}>{busy === 'ask' ? 'Asking…' : 'Ask'}</button>
      <button type="button" className="primary" disabled={!text.trim() || Boolean(busy)} onClick={() => submit('capture')}>{busy === 'capture' ? 'Capturing…' : 'Capture'}</button>
    </div>
    <span className="compact-capture-hint">⌘↵ Capture · ⌘⇧↵ Ask</span>{error && <p className="work-inline-error" role="alert">{error}</p>}
  </section>
}

export default function MyWork() {
  const [work, setWork] = useState(EMPTY_WORK)
  const [workLoading, setWorkLoading] = useState(true)
  const [workError, setWorkError] = useState('')
  const [meetings, setMeetings] = useState([])
  const [scheduleLoading, setScheduleLoading] = useState(true)
  const [scheduleError, setScheduleError] = useState('')
  const [newTitle, setNewTitle] = useState('')
  const [importUrl, setImportUrl] = useState('')
  const [addMode, setAddMode] = useState('local')
  const [addBusy, setAddBusy] = useState(false)
  const [showAdd, setShowAdd] = useState(false)
  const [mutationError, setMutationError] = useState('')

  const loadWork = useCallback((signal) => api.myWork({ signal }).then((data) => {
    if (signal.aborted) return
    setWork({ ...EMPTY_WORK, ...data }); setWorkError('')
  }).catch((err) => { if (err.name !== 'AbortError') setWorkError('Could not load work. Try refreshing.') }).finally(() => { if (!signal.aborted) setWorkLoading(false) }), [])
  const loadCalendar = useCallback((signal) => api.today({ signal }).then((data) => {
    if (signal.aborted) return
    setMeetings(data.meetings || []); setScheduleError('')
  }).catch((err) => {
    if (err.name !== 'AbortError') setScheduleError('Schedule is temporarily unavailable.')
  }).finally(() => { if (!signal.aborted) setScheduleLoading(false) }), [])
  const refresh = useCallback(signal => {
    loadWork(signal); loadCalendar(signal)
  }, [loadWork, loadCalendar])
  useCacheRefresh(refresh)

  useEffect(() => {
    const onAdd = () => { setAddMode('local'); setShowAdd(true) }
    window.addEventListener('watson:add-work', onAdd)
    return () => window.removeEventListener('watson:add-work', onAdd)
  }, [])

  const moveCard = async (itemId, requestedState, beforeId, afterId) => {
    const previous = work
    const item = work.items.find((entry) => entry.id === itemId)
    if (!item) return
    setMutationError('')
    try { setWork(moveBoardCard({ ...work, mode: 'flat' }, itemId, item.state, beforeId, afterId)) }
    catch (err) { setMutationError(err.message); return }
    const release = syncMonitor.hold()
    try { await api.moveWork(itemId, { state: item.state, before_id: beforeId ?? null, after_id: afterId ?? null }) }
    catch (err) { setWork(previous); setMutationError(`Could not move work. ${err.message}`) }
    finally { release() }
  }
  const addWork = async (event) => {
    event.preventDefault()
    const value = addMode === 'local' ? newTitle.trim() : importUrl.trim()
    if (!value || addBusy) return
    setMutationError('')
    setAddBusy(true)
    const release = syncMonitor.hold()
    try {
      if (addMode === 'local') {
        const response = await api.createWork({ title: value, state: 'next' })
        setWork((current) => ({
          ...current,
          items: [...current.items, response.work_item],
        }))
        setNewTitle(''); setShowAdd(false)
      } else {
        const response = await api.importWorkUrl(value)
        setImportUrl(''); setShowAdd(false)
        navigate(`/work/${response.work_item.id}`, { sourceBoard: 'my-work' })
      }
    } catch {
      setMutationError(addMode === 'local' ? 'Could not add local work. Please try again.' : 'Could not import this link. Check the URL and integration in Settings, then try again.')
    } finally { setAddBusy(false); release() }
  }
  const refreshAfterCapture = () => {
    syncMonitor.refresh()
  }

  return <div className="my-work-page">
    <header className="my-work-header">
      <div><p className="eyebrow">My Work</p><h1>My day</h1><p className="my-day-date">{new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })}</p></div>
      {!workLoading && !workError && <span className="my-day-count">{work.items.length} active {work.items.length === 1 ? 'task' : 'tasks'}</span>}
    </header>
    <ScheduleStrip personal meetings={meetings} loading={scheduleLoading} error={scheduleError} />
    <section className="personal-tasks" aria-labelledby="my-tasks-title">
    <div className="personal-tasks-heading"><h2 id="my-tasks-title">My tasks</h2><button type="button" onClick={() => { setAddMode('local'); setShowAdd(true) }}>+ Add work</button></div>
    {showAdd && <form className="add-work-form" onSubmit={addWork}>
      <div className="add-work-modes" role="group" aria-label="Add work mode"><button type="button" className={addMode === 'local' ? 'active' : ''} onClick={() => setAddMode('local')} aria-pressed={addMode === 'local'}>Local work</button><button type="button" className={addMode === 'import' ? 'active' : ''} onClick={() => setAddMode('import')} aria-pressed={addMode === 'import'}>Import link</button></div>
      {addMode === 'local' ? <><label htmlFor="new-work-title">New work</label><input id="new-work-title" autoFocus value={newTitle} onChange={(event) => setNewTitle(event.target.value)} placeholder="What needs your attention?" /></> : <><label htmlFor="import-work-url">GitLab MR or ClickUp task URL</label><input id="import-work-url" type="url" autoFocus value={importUrl} onChange={(event) => setImportUrl(event.target.value)} placeholder="https://gitlab…/merge_requests/42 or https://app.clickup.com/…" /><span className="add-work-import-note">Imports the linked work into Watson; it does not change ClickUp or GitLab.</span></>}
      <button type="submit" disabled={addBusy || !(addMode === 'local' ? newTitle.trim() : importUrl.trim())}>{addBusy ? (addMode === 'local' ? 'Adding…' : 'Importing…') : (addMode === 'local' ? 'Add' : 'Import')}</button><button type="button" className="quiet" disabled={addBusy} onClick={() => setShowAdd(false)}>Cancel</button>
    </form>}
    {mutationError && <p className="work-inline-error" role="alert">{mutationError}</p>}
    {workError && <p className="work-inline-error" role="alert">{workError}</p>}
    {workLoading ? <p className="work-loading">Loading your work…</p> : <PersonalTaskList items={work.items} onMove={moveCard} onOpen={(item) => navigate(`/work/${item.id}`, { sourceBoard: 'my-work' })} />}
    </section>
    <CompactCapture onCaptured={refreshAfterCapture} />
  </div>
}
