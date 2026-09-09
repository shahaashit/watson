import { useEffect, useSyncExternalStore } from 'react'
import { api } from './api.js'
import { createSyncMonitor } from './syncMonitor.js'
import { attachBoardRefreshGuard } from './boardRefreshGuard.js'

export const syncMonitor = createSyncMonitor({ fetchStatus: options => api.syncStatus(options), visibility: typeof document === 'undefined' ? { visibilityState: 'hidden' } : document })
export const useSyncStatus = () => useSyncExternalStore(syncMonitor.subscribe, syncMonitor.getSnapshot, syncMonitor.getSnapshot)
export function useCacheRefresh(load) {
  useEffect(() => syncMonitor.subscribeRefresh(load), [load])
}

export function useSyncMonitor() {
  useEffect(() => {
    const stop = syncMonitor.start()
    const detach = attachBoardRefreshGuard(document, window, syncMonitor.hold)
    return () => {
      stop(); detach()
    }
  }, [])
}

export async function runFullSync() {
  try { return await api.syncAll() }
  finally { syncMonitor.check(); syncMonitor.refresh() }
}
