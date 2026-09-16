import { useCallback, useRef, useState } from 'react'
import { api } from '../api.js'
import { syncMonitor, useCacheRefresh } from '../useSyncRefresh.js'
import useLocalWorkMutation from '../useLocalWorkMutation.js'
import RemovedWorkList from './RemovedWorkList.jsx'

export default function RemovedWorkSettings() {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [message, setMessage] = useState('')
  const restoringId = useRef(null)
  const mutation = useLocalWorkMutation('removed-work', detail => {
    setItems(current => current.filter(item => item.id !== detail.id))
    setMessage('Work restored to Watson.')
  })
  const load = useCallback(async signal => {
    setLoading(true)
    try {
      const response = await api.removedWork({ signal })
      if (!signal.aborted) { setItems(response.work_items || []); setLoadError('') }
    } catch (error) {
      if (!signal.aborted && error.name !== 'AbortError') setLoadError('Could not load removed work. Please retry.')
    } finally { if (!signal.aborted) setLoading(false) }
  }, [])
  useCacheRefresh(load)
  const restore = id => {
    mutation.run(() => {
      restoringId.current = id
      setMessage('')
      return api.restoreWork(id)
    }, 'Could not restore this work. Please retry.')
  }
  return <section className="settings-section removed-work-settings" aria-labelledby="removed-work-heading">
    <div className="settings-section-heading"><div><h2 id="removed-work-heading">Removed work</h2><p>Restore work removed from Watson. ClickUp tasks and GitLab MRs are unchanged.</p></div></div>
    <RemovedWorkList items={items} loading={loading} error={loadError} busyId={mutation.busy ? restoringId.current : null} onRestore={restore} onRetry={() => syncMonitor.refresh()} />
    {mutation.error && <p className="settings-error" role="alert">{mutation.error}</p>}
    {message && <p className="settings-success" role="status">{message}</p>}
  </section>
}
