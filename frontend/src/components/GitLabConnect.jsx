import { useEffect, useState } from 'react'
import { api } from '../api.js'

export default function GitLabConnect({ connected = false, onConnected }) {
  const [session, setSession] = useState(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [url, setUrl] = useState('')
  useEffect(() => {
    if (!session) return undefined
    let cancelled = false
    let timer
    const deadline = Date.now() + 320000
    const poll = async () => {
      try {
        const result = await api.gitlabConnectStatus(session)
        if (cancelled) return
        if (result.status === 'connected') {
          setMessage('GitLab connected. Select your repositories below.')
          setSession(null); setBusy(false); setUrl('')
          window.dispatchEvent(new Event('watson:connections-changed'))
          onConnected?.()
          return
        }
        if (result.status === 'failed' || Date.now() > deadline) throw new Error()
        timer = window.setTimeout(poll, 1200)
      } catch {
        if (!cancelled) {
          setMessage('Sign-in did not finish. Try connecting again.')
          setSession(null); setBusy(false); setUrl('')
        }
      }
    }
    poll()
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [session, onConnected])
  const connect = async () => {
    if (busy) return
    const popup = window.open('about:blank', '_blank')
    if (popup) popup.opener = null
    setBusy(true); setMessage('Opening GitLab sign-in…')
    try {
      const response = await api.connectGitlab()
      setSession(response.session_id); setUrl(response.authorization_url)
      if (popup) popup.location.href = response.authorization_url
      setMessage('Finish sign-in in your browser. Watson will reconnect automatically.')
    } catch {
      popup?.close(); setBusy(false)
      setMessage('Could not start sign-in. Check application setup or wait for the previous attempt to finish.')
    }
  }
  return <div className="integration-actions">
    <button type="button" className="settings-primary" disabled={busy} onClick={connect}>
      {busy ? 'Waiting for GitLab…' : connected ? 'Reconnect GitLab' : 'Connect GitLab'}
    </button>
    {url && <a href={url} target="_blank" rel="noreferrer">Open GitLab sign-in</a>}
    {message && <p role="status">{message}</p>}
  </div>
}
