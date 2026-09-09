import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import GitLabConnect from './GitLabConnect.jsx'
import ClickUpConnect from './ClickUpConnect.jsx'

export default function ConnectionNotice() {
  const [needsReconnect, setNeedsReconnect] = useState([])
  const refresh = useCallback(async () => {
    try {
      const response = await api.settings()
      setNeedsReconnect(['gitlab','clickup'].filter((source) => {
        const integration = response.integrations?.[source]
        return integration?.oauth_available && integration?.reauth_required
      }))
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
  if (!needsReconnect.length) return null
  return <>{needsReconnect.map((source) => <aside key={source} className="settings-section connection-notice" role="status">
    <p>{source === 'gitlab' ? 'GitLab' : 'ClickUp'} access needs renewal. Your cached work is still available.</p>
    {source === 'gitlab' ? <GitLabConnect connected onConnected={refresh} /> : <ClickUpConnect connected onConnected={refresh} />}
  </aside>)}</>
}
