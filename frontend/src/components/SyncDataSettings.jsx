import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'

function sourceName(source) {
  return { anthropic: 'AI provider', gitlab: 'GitLab', clickup: 'ClickUp', 'google-calendar': 'Google Calendar', flock: 'Flock' }[source] || source
}

function sourceState(source) {
  return source.status === 'healthy' ? 'Connected' : source.status === 'disabled' ? 'Disconnected' : source.status === 'unconfigured' ? 'Not configured' : 'Needs attention'
}

export default function SyncDataSettings({ data, compact = false, mode = 'both' }) {
  const [backupDir, setBackupDir] = useState(data?.backup_dir || '')
  const [sources, setSources] = useState([])
  const [loadingHealth, setLoadingHealth] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [retryingSource, setRetryingSource] = useState('')
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const mountedRef = useRef(false)
  const pollGenerationRef = useRef(0)
  const pollTimerRef = useRef(null)
  const pollWakeRef = useRef(null)

  useEffect(() => { setBackupDir(data?.backup_dir || '') }, [data?.backup_dir])
  const loadHealth = async (generation = pollGenerationRef.current) => {
    if (!mountedRef.current || generation !== pollGenerationRef.current) return null
    setLoadingHealth(true)
    try {
      const response = await api.syncStatus()
      if (!mountedRef.current || generation !== pollGenerationRef.current) return null
      setSources(response.sources || [])
      return response
    }
    catch {
      if (mountedRef.current && generation === pollGenerationRef.current) setMessage('Sync health is temporarily unavailable. Cached work is still available.')
      return null
    }
    finally {
      if (mountedRef.current && generation === pollGenerationRef.current) setLoadingHealth(false)
    }
  }
  useEffect(() => {
    const generation = ++pollGenerationRef.current
    mountedRef.current = true
    setSyncing(false); setRetryingSource('')
    if (mode === 'data') setLoadingHealth(false)
    else loadHealth(generation)
    return () => {
      mountedRef.current = false
      pollGenerationRef.current += 1
      if (pollTimerRef.current) window.clearTimeout(pollTimerRef.current)
      pollTimerRef.current = null
      pollWakeRef.current?.()
      pollWakeRef.current = null
    }
  }, [mode])

  const waitForPoll = () => new Promise((resolve) => {
    pollWakeRef.current = () => resolve()
    pollTimerRef.current = window.setTimeout(() => {
      pollTimerRef.current = null
      pollWakeRef.current = null
      resolve()
    }, 1200)
  })
  const pollUntilIdle = async (generation) => {
    const deadline = Date.now() + 120000
    while (mountedRef.current && generation === pollGenerationRef.current && Date.now() < deadline) {
      await waitForPoll()
      if (!mountedRef.current || generation !== pollGenerationRef.current) return null
      try {
        const status = await api.syncStatus()
        if (!mountedRef.current || generation !== pollGenerationRef.current) return null
        setSources(status.sources || [])
        if (!status.running) return status
      } catch {
        return null
      }
    }
    return null
  }
  const syncAll = async () => {
    if (syncing || retryingSource) return
    const generation = pollGenerationRef.current
    setSyncing(true); setMessage('')
    try {
      const response = await api.syncAll()
      if (!mountedRef.current || generation !== pollGenerationRef.current) return
      if (response?.skipped === 'already_running') {
        setMessage('Another sync is already running. Watching its local status…')
        const status = await pollUntilIdle(generation)
        if (!mountedRef.current || generation !== pollGenerationRef.current) return
        if (status && !status.running) setMessage('The existing sync finished.')
        else setMessage('Sync is still running. Check its cached status again shortly.')
      } else {
        setMessage('Sync finished.')
        await loadHealth(generation)
        if (!mountedRef.current || generation !== pollGenerationRef.current) return
      }
    }
    catch { if (mountedRef.current && generation === pollGenerationRef.current) setMessage('Could not complete sync. Review the affected integration and retry.') }
    finally { if (mountedRef.current && generation === pollGenerationRef.current) setSyncing(false) }
  }
  const retry = async (source) => {
    if (syncing || retryingSource) return
    const generation = pollGenerationRef.current
    setRetryingSource(source)
    setMessage('')
    try {
      await api.retrySync(source)
      if (!mountedRef.current || generation !== pollGenerationRef.current) return
      setMessage(`${sourceName(source)} retry completed.`)
      await loadHealth(generation)
    }
    catch { if (mountedRef.current && generation === pollGenerationRef.current) setMessage(`${sourceName(source)} could not retry. Review its local setup.`) }
    finally { if (mountedRef.current && generation === pollGenerationRef.current) setRetryingSource('') }
  }
  const saveBackup = async (event) => {
    event.preventDefault()
    if (saving || !backupDir.trim()) return
    setSaving(true); setMessage('')
    try { const response = await api.updateDataSettings({ backup_dir: backupDir.trim() }); setBackupDir(response.data.backup_dir); setMessage('Backup destination saved.') }
    catch { setMessage('Could not save the backup destination. It must be separate from Watson’s live data directory.') }
    finally { setSaving(false) }
  }

  return <div className={`sync-data-settings${compact ? ' settings-compact' : ''}`}>
    {mode !== 'data' && <section className="settings-section sync-settings" aria-labelledby="sync-settings-title">
      <div className="settings-section-heading"><div><p className="eyebrow">Sync</p><h2 id="sync-settings-title">Cached sources</h2><p>Sync refreshes local caches. It never applies external writes without your approval.</p></div><button className="settings-primary" type="button" onClick={syncAll} disabled={syncing || retryingSource}>{syncing ? 'Syncing…' : 'Sync now'}</button></div>
      {loadingHealth ? <p className="settings-muted">Loading source health…</p> : <div className="sync-source-list">{sources.length ? sources.map((source) => <div className="sync-source-row" key={source.source}><div><strong>{sourceName(source.source)}</strong><p>{source.message || sourceState(source)}</p>{source.last_success_at && <time dateTime={source.last_success_at}>Last cached {new Date(source.last_success_at).toLocaleString()}</time>}</div><div><span className={`integration-state ${source.status === 'healthy' ? 'configured' : ''}`}>{sourceState(source)}</span>{source.status === 'degraded' && <button type="button" onClick={() => retry(source.source)} disabled={syncing || retryingSource}>{retryingSource === source.source ? 'Retrying…' : 'Retry'}</button>}</div></div>) : <p className="settings-muted">No source status has been cached yet. Manual local work still works.</p>}</div>}
    </section>}
    {mode !== 'sync' && <section className="settings-section data-settings" aria-labelledby="data-settings-title"><div className="settings-section-heading"><div><p className="eyebrow">Data & Backup</p><h2 id="data-settings-title">Keep the live SQLite database local</h2><p>Watson’s live SQLite database stays on this Mac. The backup destination is a separate folder for daily snapshots and journal exports.</p></div></div><form className="settings-form" onSubmit={saveBackup}><label>Backup destination<input value={backupDir} onChange={(event) => setBackupDir(event.target.value)} placeholder="/Volumes/Drive/watson" disabled={saving} required /></label><div className="settings-form-actions"><button className="settings-primary" type="submit" disabled={saving}>{saving ? 'Saving…' : 'Save backup destination'}</button></div></form></section>}
    {message && <p className={message.includes('Could not') || message.includes('could not') ? 'settings-error' : 'settings-success'} role="status">{message}</p>}
  </div>
}
