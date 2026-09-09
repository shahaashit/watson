import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import GitLabConnect from './GitLabConnect.jsx'

export default function ConnectionNotice() {
  const [needsReconnect, setNeedsReconnect] = useState(false)
  const refresh = useCallback(async () => {
    try {
      const response = await api.settings()
      const gitlab = response.integrations?.gitlab
      setNeedsReconnect(Boolean(gitlab?.oauth_available && gitlab?.reauth_required))
    } catch { /* Keep cached work available. */ }
  }, [])
  useEffect(() => {
    refresh()
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') refresh()
    }, 60000)
    window.addEventListener('watson:connections-changed', refresh)
    return () => { window.clearInterval(timer); window.removeEventListener('watson:connections-changed', refresh) }
  }, [refresh])
  if (!needsReconnect) return null
  return <aside className="settings-section" role="status">
    <p>GitLab access needs renewal. Your cached work is still available.</p>
    <GitLabConnect connected onConnected={refresh} />
  </aside>
}
