import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { runFullSync, syncMonitor, useSyncStatus } from '../useSyncRefresh.js'
import RemovedWorkSettings from './RemovedWorkSettings.jsx'

function sourceName(source) {
  return { anthropic: 'AI provider', gitlab: 'GitLab', clickup: 'ClickUp', 'google-calendar': 'Google Calendar' }[source] || source
}

function sourceState(source) {
  return source.status === 'healthy' ? 'Connected' : source.status === 'disabled' ? 'Disconnected' : source.status === 'unconfigured' ? 'Not configured' : 'Needs attention'
}

export default function SyncDataSettings({ data, compact = false, mode = 'both' }) {
  const [backupDir, setBackupDir] = useState(data?.backup_dir || '')
  const { status, error: healthError } = useSyncStatus()
  const sources = (status?.sources || []).filter(source => source.source !== 'flock')
  const loadingHealth = !status && !healthError
  const [requestRunning, setSyncing] = useState(false)
  const syncing = requestRunning || status?.running
  const [retryingSource, setRetryingSource] = useState('')
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const mountedRef = useRef(false)
  const pollGenerationRef = useRef(0)

  useEffect(() => { setBackupDir(data?.backup_dir || '') }, [data?.backup_dir])
  useEffect(() => {
    ++pollGenerationRef.current
    mountedRef.current = true
    setSyncing(false); setRetryingSource('')
    return () => {
      mountedRef.current = false
      pollGenerationRef.current += 1
    }
  }, [mode])

  const syncAll = async () => {
    if (syncing || retryingSource) return
    const generation = pollGenerationRef.current
    setSyncing(true); setMessage('')
    try {
      const response = await runFullSync()
      if (!mountedRef.current || generation !== pollGenerationRef.current) return
      if (response?.skipped === 'already_running') {
        setMessage('A sync is already running. Progress is shown above; views update automatically when it finishes.')
      } else {
        setMessage('Sync attempt finished. See the latest result above and connection details below.')
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
      syncMonitor.check(); syncMonitor.refresh()
    }
    catch { if (mountedRef.current && generation === pollGenerationRef.current) setMessage(`${sourceName(source)} could not retry. Review its local setup.`) }
    finally { if (mountedRef.current && generation === pollGenerationRef.current) setRetryingSource('') }
  }
  const saveBackup = async (event) => {
    // This changes the backup destination, never the location of the live SQLite database.
    event.preventDefault()
    if (saving || !backupDir.trim()) return
    setSaving(true); setMessage('')
    try { const response = await api.updateDataSettings({ backup_dir: backupDir.trim() }); setBackupDir(response.data.backup_dir); setMessage('Backup destination saved.') }
    catch { setMessage('Could not save the backup destination. It must be separate from Watson’s live data directory.') }
    finally { setSaving(false) }
  }

  return <div className={`sync-data-settings${compact ? ' settings-compact' : ''}`}>
    {mode !== 'data' && <section className="settings-section sync-settings" aria-labelledby="sync-settings-title">
      <div className="settings-section-heading"><div><p className="eyebrow">Sync</p><h2 id="sync-settings-title">Connected services</h2><p>Refresh the work and updates available in Watson.</p></div><button className="settings-primary" type="button" onClick={syncAll} disabled={syncing || retryingSource}>{syncing ? 'Syncing…' : 'Sync now'}</button></div>
      {loadingHealth ? <p className="settings-muted">Checking connections…</p> : <div className="sync-source-list">{sources.length ? sources.map((source) => <div className="sync-source-row" key={source.source}><div><strong>{sourceName(source.source)}</strong><p>{source.message || sourceState(source)}</p>{source.last_success_at && <time dateTime={source.last_success_at}>Last updated {new Date(source.last_success_at).toLocaleString()}</time>}</div><div><span className={`integration-state ${source.status === 'healthy' ? 'configured' : ''}`}>{sourceState(source)}</span>{source.status === 'degraded' && <button type="button" onClick={() => retry(source.source)} disabled={syncing || retryingSource}>{retryingSource === source.source ? 'Retrying…' : 'Retry'}</button>}</div></div>) : <p className="settings-muted">No updates yet. You can still add work and notes.</p>}</div>}
    </section>}
    {mode !== 'sync' && <section className="settings-section data-settings" aria-labelledby="data-settings-title"><div className="settings-section-heading"><div><p className="eyebrow">Data & Backup</p><h2 id="data-settings-title">Keep your work backed up</h2><p>Your work stays on this Mac. Choose a separate folder for daily backups and exported notes.</p></div></div><form className="settings-form" onSubmit={saveBackup}><label>Backup destination<input value={backupDir} onChange={(event) => setBackupDir(event.target.value)} placeholder="/Volumes/Drive/watson" disabled={saving} required /></label><div className="settings-form-actions"><button className="settings-primary" type="submit" disabled={saving}>{saving ? 'Saving…' : 'Save backup destination'}</button></div></form></section>}
    {mode !== 'sync' && <RemovedWorkSettings />}
    {message && <p className={message.includes('Could not') || message.includes('could not') ? 'settings-error' : 'settings-success'} role="status">{message}</p>}
  </div>
}
