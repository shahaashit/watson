import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { navigate } from '../routing.js'
import PersonLane from '../components/PersonLane.jsx'
import { readMeMode, withPermanentLanes, writeMeMode } from '../teamLanes.js'

export default function Team() {
  const [lanes, setLanes] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [meMode, setMeMode] = useState(() => readMeMode())

  const load = useCallback((signal) => api.teamWork({ signal, meMode }).then((data) => {
    setLanes(withPermanentLanes(data.lanes)); setError('')
  }).catch((err) => {
    if (err.name !== 'AbortError') setError('Could not load the team board. Cached work will be available after retrying.')
  }).finally(() => { if (!signal?.aborted) setLoading(false) }), [meMode])

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    return () => controller.abort()
  }, [load])

  const setLaneItems = (index, items) => setLanes((current) => current.map((lane, laneIndex) => laneIndex === index ? { ...lane, items } : lane))
  const toggleMeMode = () => {
    const next = !meMode
    writeMeMode(next)
    setLoading(true)
    setMeMode(next)
  }

  return <div className="team-page">
    <header className="team-header"><div><p className="eyebrow">Team</p><h1>A clear view of your team.</h1><p>See what everyone is working on. Drag cards to set your own priorities.</p></div><div className="team-header-actions"><button type="button" className={`team-me-mode${meMode ? ' active' : ''}`} role="switch" aria-checked={meMode} onClick={toggleMeMode}><span aria-hidden="true" />Me mode</button><button type="button" className="team-retry" onClick={() => { setLoading(true); load() }}>Refresh</button></div></header>
    {loading ? <p className="work-loading">Loading the team board…</p> : error ? <div className="team-error" role="alert"><p>{error}</p><button type="button" onClick={() => { setLoading(true); load() }}>Try again</button></div> : <div className="team-lanes" aria-label="Team work lanes">
      {lanes.map((lane, index) => <PersonLane key={lane.person?.id || lane.name} lane={lane} onItemsChange={(items) => setLaneItems(index, items)} onOpen={(item) => navigate(`/work/${item.id}`, { sourceBoard: 'team' })} />)}
    </div>}
  </div>
}
