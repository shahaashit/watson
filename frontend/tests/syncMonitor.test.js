import test from 'node:test'
import assert from 'node:assert/strict'
import { createSyncMonitor } from '../src/syncMonitor.js'

const tick = () => new Promise(resolve => setImmediate(resolve))
function fixture() {
  const visibility = new EventTarget()
  visibility.visibilityState = 'visible'
  let response = { running: false, last_sync_at: 'first', sources: [] }
  let calls = 0
  const timers = new Map()
  let id = 0
  const monitor = createSyncMonitor({
    visibility,
    fetchStatus: async () => { calls++; return response },
    schedule: (fn, ms) => { timers.set(++id, { fn, ms }); return id },
    cancel: key => timers.delete(key),
  })
  return { monitor, visibility, timers, calls: () => calls, respond: value => { response = value } }
}

test('completed sync refreshes every subscriber once, unchanged polls do not', async () => {
  const f = fixture()
  let board = 0, log = 0
  f.monitor.subscribeRefresh(() => board++)
  f.monitor.subscribeRefresh(() => log++)
  const stop = f.monitor.start()
  await tick()
  assert.equal(board, 2)
  await f.monitor.check()
  assert.equal(log, 2)
  f.respond({ running: false, last_sync_at: 'second', sources: [] })
  await f.monitor.check()
  assert.equal(board, 3)
  assert.equal(log, 3)
  assert.equal([...f.timers.values()].filter(t => t.ms === 3000).length, 1)
  stop()
  assert.equal(f.timers.size, 0)
})

test('hidden tabs stop polling and refresh cached views when visible again', async () => {
  const f = fixture()
  let refreshes = 0
  f.monitor.subscribeRefresh(() => refreshes++)
  const stop = f.monitor.start()
  await tick()
  f.visibility.visibilityState = 'hidden'
  f.visibility.dispatchEvent(new Event('visibilitychange'))
  await f.monitor.check()
  assert.equal(f.calls(), 1)
  f.visibility.visibilityState = 'visible'
  f.visibility.dispatchEvent(new Event('visibilitychange'))
  await tick()
  assert.equal(f.calls(), 2)
  assert.equal(refreshes, 3)
  stop()
})

test('dragging and pending writes abort stale reads and defer refresh until all finish', () => {
  const f = fixture()
  const signals = []
  f.monitor.subscribeRefresh(signal => signals.push(signal))
  const releaseDrag = f.monitor.hold()
  const releaseWrite = f.monitor.hold()
  assert.equal(signals[0].aborted, true)
  f.monitor.refresh()
  releaseDrag()
  assert.equal(signals.length, 1)
  releaseWrite()
  assert.equal(signals.length, 2)
  assert.equal(signals[1].aborted, false)
  releaseWrite()
  assert.equal(signals.length, 2)
})

test('concurrent checks never overlap status requests', async () => {
  let complete
  let calls = 0
  const visibility = new EventTarget()
  visibility.visibilityState = 'visible'
  const monitor = createSyncMonitor({ visibility, fetchStatus: () => { calls++; return new Promise(resolve => { complete = resolve }) } })
  const stop = monitor.start()
  monitor.check()
  assert.equal(calls, 1)
  complete({ running: true, last_sync_at: null, sources: [] })
  await tick()
  assert.equal(monitor.getSnapshot().status.running, true)
  stop()
})

test('status errors preserve cached health and recover on the next successful poll', async () => {
  let fail = false
  const status = { running: false, last_sync_at: 'first', sources: [{ source: 'gitlab', status: 'healthy' }] }
  const visibility = new EventTarget()
  visibility.visibilityState = 'visible'
  const monitor = createSyncMonitor({ visibility, fetchStatus: async () => {
    if (fail) throw new Error('offline')
    return status
  } })
  const stop = monitor.start()
  await tick()
  fail = true
  await monitor.check()
  assert.deepEqual(monitor.getSnapshot().status, status)
  assert.match(monitor.getSnapshot().error, /unavailable/)
  fail = false
  await monitor.check()
  assert.equal(monitor.getSnapshot().error, '')
  stop()
})

test('a running-to-idle transition refreshes even when the completion stamp is unchanged', async () => {
  const f = fixture()
  f.respond({ running: true, last_sync_at: 'first', sources: [] })
  let reads = 0
  const unsubscribe = f.monitor.subscribeRefresh(() => reads++)
  const stop = f.monitor.start()
  await tick()
  f.respond({ running: false, last_sync_at: 'first', sources: [] })
  await f.monitor.check()
  assert.equal(reads, 2)
  unsubscribe()
  f.monitor.refresh()
  assert.equal(reads, 2)
  stop()
})
