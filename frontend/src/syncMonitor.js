// One local status reader, with cancellable cache subscriptions. Never starts a sync.
export function createSyncMonitor({ fetchStatus, visibility, schedule = setTimeout, cancel = clearTimeout }) {
  const initializedAt = Date.now()
  let snapshot = { status: null, error: '' }
  const listeners = new Set()
  const readers = new Map()
  let active = false, timer = null, request = null, holds = 0, pending = false
  const visible = () => visibility.visibilityState !== 'hidden'
  const emit = () => listeners.forEach(listener => listener())
  const read = (listener) => {
    readers.get(listener)?.abort()
    const controller = new AbortController()
    readers.set(listener, controller)
    listener(controller.signal)
  }
  const refresh = () => {
    if (holds || !visible()) { pending = true; return }
    pending = false
    readers.forEach((_, listener) => read(listener))
  }
  const signature = status => JSON.stringify([status?.last_sync_at, (status?.sources || []).map(source => [source.source, source.last_success_at, source.status])])
  const check = async () => {
    if (!active || !visible() || request) return
    cancel(timer)
    const controller = new AbortController()
    request = controller
    const timeout = schedule(() => controller.abort(), 15000)
    try {
      const status = await fetchStatus({ signal: controller.signal })
      if (controller.signal.aborted || !active) return
      const previous = snapshot.status
      snapshot = { status, error: '' }
      emit()
      // An idle baseline is not a completion event. Preserve the first cache
      // request unless a sync actually completed while this page was starting.
      const completedDuringStartup = !previous && [status.last_sync_at, ...(status.sources || []).map(source => source.last_success_at)]
        .some(stamp => Date.parse(stamp) >= initializedAt)
      if (!status.running && (completedDuringStartup || (previous && (previous.running || signature(previous) !== signature(status))))) refresh()
    } catch {
      if (active && visible()) {
        snapshot = { ...snapshot, error: 'Sync status unavailable. Showing cached data; reconnecting automatically.' }
        emit()
      }
    } finally {
      cancel(timeout)
      if (request === controller) request = null
      if (active && visible()) timer = schedule(check, 3000)
    }
  }
  const onVisibility = () => {
    cancel(timer)
    if (visible()) { refresh(); check() }
    else request?.abort()
  }
  return {
    getSnapshot: () => snapshot,
    subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener) },
    subscribeRefresh: listener => {
      readers.set(listener, null)
      if (!holds && visible()) read(listener)
      else pending = true
      return () => { readers.get(listener)?.abort(); readers.delete(listener) }
    },
    refresh, check,
    hold: () => {
      holds++
      readers.forEach(controller => controller?.abort())
      pending = true
      let released = false
      return () => {
        if (released) return
        released = true
        holds--
        if (!holds && pending) refresh()
      }
    },
    start: () => {
      active = true
      visibility.addEventListener('visibilitychange', onVisibility)
      check()
      return () => {
        active = false
        cancel(timer)
        request?.abort()
        visibility.removeEventListener('visibilitychange', onVisibility)
      }
    },
  }
}
