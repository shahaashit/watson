import { useCallback, useState } from 'react'
import { api } from '../api.js'
import { navigate } from '../routing.js'
import PersonLane from '../components/PersonLane.jsx'
import QuickNotes, { NotesToggle } from '../components/QuickNotes.jsx'
import { readNotesOpen, writeNotesOpen } from '../notesPanel.js'
import { readMeMode, withPermanentLanes, writeMeMode } from '../teamLanes.js'
import { syncMonitor, useCacheRefresh } from '../useSyncRefresh.js'

export default function Team() {
  const [lanes, setLanes] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [meMode, setMeMode] = useState(() => readMeMode())
  const [notesOpen, setNotesOpen] = useState(() => readNotesOpen())

  const load = useCallback((signal) => api.teamWork({ signal, meMode }).then((data) => {
    if (signal?.aborted) return
    setLanes(withPermanentLanes(data.lanes)); setError('')
  }).catch((err) => {
    if (err.name !== 'AbortError') setError('Could not load the team board. Cached work will be available after retrying.')
  }).finally(() => { if (!signal?.aborted) setLoading(false) }), [meMode])

  useCacheRefresh(load)

  const setLaneItems = (index, items) => setLanes((current) => current.map((lane, laneIndex) => laneIndex === index ? { ...lane, items } : lane))
  const toggleMeMode = () => {
    const next = !meMode
    writeMeMode(next)
    setLoading(true)
    setMeMode(next)
  }
  const toggleNotes = () => setNotesOpen((open) => {
    writeNotesOpen(!open)
    return !open
  })

  return <div className="team-page">
    <header className="team-header"><div><p className="eyebrow">Team</p><h1>A clear view of your team.</h1><p>See what everyone is working on. Drag cards to set your own priorities.</p></div><div className="team-header-actions"><button type="button" className={`team-me-mode${meMode ? ' active' : ''}`} role="switch" aria-checked={meMode} onClick={toggleMeMode}><span aria-hidden="true" />Me mode</button><NotesToggle open={notesOpen} onToggle={toggleNotes} /></div></header>
    {error && <div className="team-error" role="alert"><p>{error}</p><button type="button" onClick={() => syncMonitor.refresh()}>Try again</button></div>}
    <div className={`board-split${notesOpen ? ' with-notes' : ''}`}>
      <div className="board-split-main">
        {loading ? <p className="work-loading">Loading the team board…</p> : <div className="team-lanes" aria-label="Team work lanes">
          {lanes.map((lane, index) => <PersonLane key={lane.person?.id || lane.name} lane={lane} onItemsChange={(items) => setLaneItems(index, items)} onOpen={(item) => navigate(`/work/${item.id}`, { sourceBoard: 'team' })} />)}
        </div>}
      </div>
      {notesOpen && <QuickNotes onClose={toggleNotes} />}
    </div>
  </div>
}
