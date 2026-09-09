import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { navigate } from '../routing.js'
import ProfileSettings from '../components/ProfileSettings.jsx'
import IntegrationSettings from '../components/IntegrationSettings.jsx'
import PeopleSettings from '../components/PeopleSettings.jsx'
import SyncDataSettings from '../components/SyncDataSettings.jsx'

const SECTIONS = [
  ['profile', 'Profile'],
  ['integrations', 'Integrations'],
  ['people', 'People'],
  ['sync', 'Sync'],
  ['data', 'Data & Backup'],
]

export default function Settings({ initialSection = 'profile' }) {
  const [snapshot, setSnapshot] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true); setError('')
    try { setSnapshot(await api.settings()) }
    catch { setError('Settings are temporarily unavailable. Your local work remains available; retry when Watson is ready.') }
    finally { setLoading(false) }
  }, [])
  useEffect(() => { load() }, [load])
  const select = (section) => navigate(section === 'profile' ? '/settings' : `/settings/${section}`)

  const section = SECTIONS.some(([key]) => key === initialSection) ? initialSection : 'profile'
  const content = () => {
    if (loading) return <p className="settings-muted">Loading Settings…</p>
    if (error) return <div className="settings-recoverable-error" role="alert"><p>{error}</p><button type="button" onClick={load}>Retry Settings</button></div>
    if (section === 'profile') return <ProfileSettings profile={snapshot?.profile} onSaved={(profile) => setSnapshot((current) => ({ ...current, profile }))} />
    if (section === 'integrations') return <IntegrationSettings integrations={snapshot?.integrations} onChanged={load} />
    if (section === 'people') return <PeopleSettings />
    return <SyncDataSettings data={snapshot?.data} mode={section} />
  }

  return <div className="settings-page">
    <header className="settings-page-header"><div><p className="eyebrow">Settings</p><h1>Make Watson yours.</h1><p>Your profile, AI provider and preferences, all in one place. Disconnect or reconnect services whenever you need.</p></div></header>
    <div className="settings-layout"><nav className="settings-nav" aria-label="Settings sections">{SECTIONS.map(([key, label]) => <button type="button" key={key} className={section === key ? 'active' : ''} onClick={() => select(key)}>{label}</button>)}</nav><div className="settings-content">{content()}</div></div>
  </div>
}
