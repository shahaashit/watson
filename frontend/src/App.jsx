import { useCallback, useEffect, useState } from 'react'
import ConnectionNotice from './components/ConnectionNotice.jsx'
import { api } from './api.js'
import { navigate, parseRoute, subscribeRoute } from './routing.js'
import MyWork from './views/MyWork.jsx'
import Team from './views/Team.jsx'
import WorkDetail from './views/WorkDetail.jsx'
import Log from './views/Log.jsx'
import Settings from './views/Settings.jsx'
import Onboarding from './views/Onboarding.jsx'
import CommandBar from './components/CommandBar.jsx'
import useDismissibleLayer from './useDismissibleLayer.js'

const VIEWS = [
  { key: 'my-work', label: 'My Work', path: '/my-work' },
  { key: 'team', label: 'Team', path: '/team' },
  { key: 'log', label: 'Log', path: '/log' },
]

export default function App() {
  const [route, setRoute] = useState(() => parseRoute(window.location.pathname))
  const [logQuery, setLogQuery] = useState('')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [onboarding, setOnboarding] = useState(null)
  const [onboardingLoading, setOnboardingLoading] = useState(true)
  const [onboardingError, setOnboardingError] = useState('')
  const settingsMenuRef = useDismissibleLayer({ open: settingsOpen, onDismiss: () => setSettingsOpen(false) })

  useEffect(() => subscribeRoute(setRoute), [])
  const loadOnboarding = useCallback(async () => {
    setOnboardingLoading(true); setOnboardingError('')
    try { setOnboarding(await api.onboarding()) }
    catch { setOnboardingError('Setup status is temporarily unavailable. Your local work remains available; retry when ready.') }
    finally { setOnboardingLoading(false) }
  }, [])
  useEffect(() => { loadOnboarding() }, [loadOnboarding])
  useEffect(() => {
    if (onboardingLoading || onboardingError || !onboarding) return
    if (!onboarding.completed && route.view !== 'onboarding') navigate('/onboarding')
    if (onboarding.completed && route.view === 'onboarding') navigate('/my-work')
  }, [onboarding, onboardingError, onboardingLoading, route.view])

  const setView = useCallback((view) => {
    const target = VIEWS.find((item) => item.key === view)
    if (target) navigate(target.path)
  }, [])
  const syncAll = useCallback(() => api.syncAll().finally(() => window.dispatchEvent(new Event('watson:today-dirty'))), [])
  const addWork = () => {
    setView('my-work')
    window.setTimeout(() => window.dispatchEvent(new Event('watson:add-work')), 0)
  }

  const renderView = () => {
    if (!onboardingLoading && onboarding && !onboarding.completed && route.view !== 'onboarding') return <Onboarding initialState={onboarding} onComplete={setOnboarding} />
    if (route.view === 'my-work') return <MyWork />
    if (route.view === 'log') return <Log initialQuery={logQuery} />
    if (route.view === 'team') return <Team />
    if (route.view === 'onboarding') return <Onboarding initialState={onboarding} onComplete={setOnboarding} />
    if (route.view === 'settings') return <Settings initialSection={route.section} />
    if (route.view === 'work-detail') return <WorkDetail workItemId={route.workItemId} />
    return <MyWork />
  }

  return <div className="app">
    <ConnectionNotice />
    <nav className="nav" aria-label="Primary navigation">
      <button className="brand" onClick={() => setView('my-work')} aria-label="Watson home">
        <svg className="brand-mark" viewBox="0 0 32 32" width="22" height="22" aria-hidden="true"><path d="M 6 9 L 11 23 L 16 15 L 21 23 L 26 9" fill="none" stroke="currentColor" strokeWidth="2.8" strokeLinecap="round" strokeLinejoin="round" /></svg>
        <span className="brand-text">Watson</span>
      </button>
      {VIEWS.map((view) => <button key={view.key} className={`nav-btn ${route.view === view.key ? 'active' : ''}`} onClick={() => navigate(view.path)}>{view.label}</button>)}
      <div className="nav-actions">
        <button type="button" className="nav-add-work" onClick={addWork}>
          <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 4v12M4 10h12" /></svg>
          <span>Add Work</span>
        </button>
        <button type="button" className="nav-search" aria-label="Search Watson" onClick={() => window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', metaKey: true }))}>
          <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="8.5" cy="8.5" r="4.75" /><path d="m12 12 4 4" /></svg>
          <span>Search</span><kbd>⌘K</kbd>
        </button>
        <div className="nav-overflow" ref={settingsMenuRef}>
          <button type="button" className="nav-more" onClick={() => setSettingsOpen((open) => !open)} aria-expanded={settingsOpen} aria-haspopup="menu" aria-label="Open utility menu" title="More actions">
            <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="4" cy="10" r="1.25" /><circle cx="10" cy="10" r="1.25" /><circle cx="16" cy="10" r="1.25" /></svg>
          </button>
          {settingsOpen && <div className="nav-overflow-menu" role="menu">
            <button type="button" role="menuitem" onClick={() => { navigate('/settings'); setSettingsOpen(false) }}>
              <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="3" /><path d="M10 2.75v1.5M10 15.75v1.5M2.75 10h1.5M15.75 10h1.5M4.88 4.88l1.06 1.06M14.06 14.06l1.06 1.06M15.12 4.88l-1.06 1.06M5.94 14.06l-1.06 1.06" /></svg>
              <span><strong>Settings</strong><small>Connections and preferences</small></span>
            </button>
            <button type="button" role="menuitem" onClick={() => { syncAll(); setSettingsOpen(false) }}>
              <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M15.5 6.5V3.75h-2.75M4.5 13.5v2.75h2.75M14.7 7A5.25 5.25 0 0 0 5.2 6M5.3 13a5.25 5.25 0 0 0 9.5 1" /></svg>
              <span><strong>Sync now</strong><small>Refresh connected services</small></span>
            </button>
          </div>}
        </div>
      </div>
    </nav>
    {onboardingError && <div className="app-bootstrap-error" role="alert"><span>{onboardingError}</span><button type="button" onClick={loadOnboarding} disabled={onboardingLoading}>{onboardingLoading ? 'Retrying…' : 'Retry'}</button></div>}
    <main className="main">{renderView()}</main>
    <CommandBar setView={setView} syncAll={syncAll} setLogQuery={setLogQuery}
      addWork={addWork} navigateTo={navigate} />
  </div>
}
