import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'

export default function ClickUpConnect({ connected = false, onConnected }) {
  const [session, setSession] = useState(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [url, setUrl] = useState('')
  const mounted = useRef(false)
  const connecting = useRef(false)
  const callback = useRef(onConnected)
  useEffect(() => { callback.current = onConnected }, [onConnected])
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  useEffect(() => {
    if (!session) return undefined
    let cancelled = false
    let timer
    const deadline = Date.now() + 320000
    const finish = (text) => {
      setMessage(text)
      setSession(null); setBusy(false); setUrl('')
      connecting.current = false
    }
    const poll = async () => {
      let result
      try {
        result = await api.clickupConnectStatus(session)
        if (cancelled) return
        if (result.status === 'failed' || Date.now() > deadline) throw new Error()
      } catch {
        if (!cancelled) finish('Sign-in did not finish. Try connecting again.')
        return
      }
      if (result.status === 'connected') {
        finish('ClickUp connected. Select your Workspace and List below.')
        window.dispatchEvent(new Event('watson:connections-changed'))
        callback.current?.()
        return
      }
      timer = window.setTimeout(poll, 1200)
    }
    poll()
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [session])
  const connect = async () => {
    if (connecting.current) return
    connecting.current = true
    const popup = window.open('about:blank', '_blank')
    if (popup) popup.opener = null
    setBusy(true); setMessage('Opening ClickUp sign-in…')
    try {
      const response = await api.connectClickup()
      if (!mounted.current) { popup?.close(); return }
      setSession(response.session_id); setUrl(response.authorization_url)
      if (popup) popup.location.href = response.authorization_url
      setMessage('Finish sign-in in your browser, then select your Workspace and List here.')
    } catch {
      popup?.close()
      if (!mounted.current) return
      connecting.current = false
      setSession(null); setUrl(''); setBusy(false)
      setMessage('Could not start sign-in. Check application setup or wait for the previous attempt to finish.')
    }
  }
  return <div className="integration-actions">
    <button type="button" className="settings-primary" disabled={busy} onClick={connect}>
      {busy ? 'Waiting for ClickUp…' : connected ? 'Reconnect ClickUp' : 'Connect ClickUp'}
    </button>
    {url && <a href={url} target="_blank" rel="noreferrer">Open ClickUp sign-in</a>}
    {message && <p role="status">{message}</p>}
  </div>
}
