import { useCallback, useState } from 'react'
import { api } from '../api.js'
import { navigate } from '../routing.js'
import PersonLane from '../components/PersonLane.jsx'
import ScheduleStrip from '../components/ScheduleStrip.jsx'
import CompactCapture from '../components/CompactCapture.jsx'
import QuickNotes, { NotesToggle } from '../components/QuickNotes.jsx'
import { useNotesPanel } from '../notesPanel.js'
import { readMeMode, withPermanentLanes, writeMeMode } from '../teamLanes.js'
import { syncMonitor, useCacheRefresh } from '../useSyncRefresh.js'

export default function Team() {
  const [lanes, setLanes] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [meMode, setMeMode] = useState(() => readMeMode())
  const [meetings, setMeetings] = useState([])
  const [scheduleLoading, setScheduleLoading] = useState(true)
  const [scheduleError, setScheduleError] = useState('')
  const notes = useNotesPanel()

  const loadLanes = useCallback((signal) => api.teamWork({ signal, meMode }).then((data) => {
    if (signal?.aborted) return
    setLanes(withPermanentLanes(data.lanes)); setError('')
  }).catch((err) => {
    if (err.name !== 'AbortError') setError('Could not load the team board. Cached work will be available after retrying.')
  }).finally(() => { if (!signal?.aborted) setLoading(false) }), [meMode])
  const loadCalendar = useCallback((signal) => api.today({ signal }).then((data) => {
    if (signal?.aborted) return
    setMeetings(data.meetings || []); setScheduleError('')
  }).catch((err) => {
    if (err.name !== 'AbortError') setScheduleError('Schedule is temporarily unavailable.')
  }).finally(() => { if (!signal?.aborted) setScheduleLoading(false) }), [])
  const load = useCallback((signal) => {
    loadLanes(signal); loadCalendar(signal)
  }, [loadLanes, loadCalendar])

  useCacheRefresh(load)

  const setLaneItems = (index, items) => setLanes((current) => current.map((lane, laneIndex) => laneIndex === index ? { ...lane, items } : lane))
  const toggleMeMode = () => {
    const next = !meMode
    writeMeMode(next)
    setLoading(true)
    setMeMode(next)
  }

  return <div className="team-page">
    <header className="team-header"><div><p className="eyebrow">Home</p><h1>A clear view of your team.</h1><p>See what everyone is working on. Drag cards to set your own priorities.</p></div><div className="team-header-actions"><button type="button" className={`team-me-mode${meMode ? ' active' : ''}`} role="switch" aria-checked={meMode} onClick={toggleMeMode}><span aria-hidden="true" />Me mode</button><NotesToggle open={notes.open} onToggle={notes.toggle} /></div></header>
    {error && <div className="team-error" role="alert"><p>{error}</p><button type="button" onClick={() => syncMonitor.refresh()}>Try again</button></div>}
    <div className={`board-split${notes.mounted ? ' with-notes' : ''}`}>
      <div className="board-split-main">
        <ScheduleStrip personal meetings={meetings} loading={scheduleLoading} error={scheduleError} />
        {loading ? <p className="work-loading">Loading the team board…</p> : <div className="team-lanes" aria-label="Team work lanes">
          {lanes.map((lane, index) => <PersonLane key={lane.person?.id || lane.name} lane={lane} onItemsChange={(items) => setLaneItems(index, items)} onOpen={(item) => navigate(`/work/${item.id}`)} onAdd={lane.person?.is_self ? () => window.dispatchEvent(new Event('watson:add-work')) : undefined} />)}
        </div>}
        <CompactCapture onCaptured={() => syncMonitor.refresh()} />
      </div>
      {notes.mounted && <QuickNotes closing={!notes.open} onClose={notes.toggle} />}
    </div>
  </div>
}
