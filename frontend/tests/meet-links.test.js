import test from 'node:test'
import assert from 'node:assert/strict'
import { api, ApiError } from '../src/api.js'
import { renderJsx } from './renderJsx.js'

test('Meet API reads local readiness and posts only an explicit request ID', async t => {
  const calls = []
  t.mock.method(globalThis, 'fetch', async (path, options) => {
    calls.push([path, options])
    return { ok: true, json: async () => ({ url: 'https://meet.google.com/abc-defg-hij' }) }
  })
  const signal = new AbortController().signal
  await api.meetLinkStatus({ signal })
  await api.createMeetLink('sample-request')
  assert.equal(calls[0][0], '/api/meet-links/status')
  assert.equal(calls[0][1].signal, signal)
  assert.equal(calls[1][0], '/api/meet-links')
  assert.equal(calls[1][1].method, 'POST')
  assert.deepEqual(JSON.parse(calls[1][1].body), { request_id: 'sample-request' })
})

test('ApiError preserves only validated structured fields without breaking string detail', async t => {
  t.mock.method(globalThis, 'fetch', async () => ({ ok: false, status: 403, statusText: 'Forbidden', json: async () => ({
    detail: { code: 'reconnect_required', message: 'Reconnect Google to enable Meet.', internal: 'never expose' },
  }) }))
  await assert.rejects(api.createMeetLink('sample-request'), error => {
    assert.ok(error instanceof ApiError)
    assert.equal(error.code, 'reconnect_required')
    assert.deepEqual(error.detailObject, { code: 'reconnect_required', message: 'Reconnect Google to enable Meet.' })
    assert.equal(error.detail, undefined)
    assert.equal(error.message, 'Reconnect Google to enable Meet.')
    return true
  })
  assert.equal(new ApiError('Conflict', 409, 'Conflict').detail, 'Conflict')
  assert.equal(new ApiError('Invalid', 500, { code: {}, message: ['unsafe'] }).detailObject, undefined)
})

async function controller(options = {}) {
  const { createMeetLinkController } = await import('../src/meetLinkController.js')
  let counter = 0
  const instance = createMeetLinkController({
    create: async () => ({ url: 'https://meet.google.com/abc-defg-hij' }),
    copy: async () => {}, uuid: () => `request-${++counter}`, ...options,
  })
  const unsubscribe = instance.subscribe(() => {})
  return { instance, unsubscribe }
}

test('Meet creation blocks duplicate clicks and another deliberate success uses a new ID', async () => {
  let finish
  const calls = []
  const { instance } = await controller({ create: id => { calls.push(id); return new Promise(resolve => { finish = resolve }) } })
  assert.deepEqual(calls, [])
  const first = instance.start()
  await instance.start()
  assert.deepEqual(calls, ['request-1'])
  finish({ url: 'https://meet.google.com/abc-defg-hij' })
  await first
  assert.equal(instance.getSnapshot().copied, true)
  const second = instance.start()
  assert.deepEqual(calls, ['request-1', 'request-2'])
  finish({ url: 'https://meet.google.com/klm-nopq-rst' })
  await second
})

test('network uncertainty reuses the persisted request ID after remount/reload; no automatic retry', async () => {
  const values = new Map()
  const storage = { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value) }
  const calls = []
  const { instance, unsubscribe } = await controller({ storage, create: async id => { calls.push(id); throw new Error('offline') } })
  await instance.start()
  assert.deepEqual(calls, ['request-1'])
  assert.equal(instance.getSnapshot().code, 'uncertain')
  unsubscribe()
  const { instance: recovered } = await controller({ storage, uuid: () => 'must-not-use', create: async id => { calls.push(id); return { url: 'https://meet.google.com/abc-defg-hij' } } })
  assert.deepEqual(calls, ['request-1'])
  await recovered.start()
  assert.deepEqual(calls, ['request-1', 'request-1'])
})

for (const code of ['reconnect_required', 'api_disabled', 'permission_denied', 'failed']) {
  test(`confirmed ${code} clears the persisted ID so a deliberate retry can recover`, async () => {
    const values = new Map()
    const storage = { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value) }
    const calls = []
    const { instance } = await controller({ storage, create: async id => {
      calls.push(id)
      throw new ApiError('Rejected', 403, { code, message: 'Request rejected.' })
    } })
    await instance.start()
    assert.equal(instance.getSnapshot().requestId, '')
    const { instance: recovered } = await controller({ storage, uuid: () => 'request-2', create: async id => {
      calls.push(id)
      return { url: 'https://meet.google.com/abc-defg-hij' }
    } })
    assert.deepEqual(calls, ['request-1'])
    await recovered.start()
    assert.deepEqual(calls, ['request-1', 'request-2'])
    assert.equal(recovered.getSnapshot().copied, true)
    assert.equal(recovered.getSnapshot().url, '')
  })
}

for (const code of ['in_progress', 'uncertain', 'unknown_failure']) {
  test(`${code} preserves the ID on deliberate retry`, async () => {
    const calls = []
    const { instance } = await controller({ create: async id => {
      calls.push(id)
      if (calls.length === 1) throw new ApiError('Unconfirmed', 409, { code, message: 'Unconfirmed result.' })
      return { url: 'https://meet.google.com/abc-defg-hij' }
    } })
    await instance.start()
    await instance.start()
    assert.deepEqual(calls, ['request-1', 'request-1'])
  })
}

test('blocked clipboard retains URL and Copy retries without creating another meeting', async () => {
  let creates = 0, copies = 0
  const { instance } = await controller({ create: async () => { creates++; return { url: 'https://meet.google.com/abc-defg-hij' } }, copy: async () => { if (++copies === 1) throw new Error('blocked') } })
  await instance.start()
  assert.equal(instance.getSnapshot().url, 'https://meet.google.com/abc-defg-hij')
  assert.equal(instance.getSnapshot().copied, false)
  await instance.copyAgain()
  assert.equal(instance.getSnapshot().copied, true)
  assert.equal(creates, 1)
})

test('unmount during creation retains result without writing to clipboard', async () => {
  let finish, copied = false
  const { instance, unsubscribe } = await controller({ create: () => new Promise(resolve => { finish = resolve }), copy: async () => { copied = true } })
  const pending = instance.start()
  unsubscribe()
  finish({ url: 'https://meet.google.com/abc-defg-hij' })
  await pending
  assert.equal(instance.getSnapshot().url, 'https://meet.google.com/abc-defg-hij')
  assert.equal(copied, false)
})

test('empty calendar still offers explicit Meet control and no-invites explanation', async () => {
  const html = await renderJsx('src/components/ScheduleStrip.jsx', { meetings: [] })
  assert.match(html, /New Meet · Copy link/)
  assert.match(html, /No invitations or calendar events/)
  assert.match(html, /No meetings on the calendar/)
})

test('successful copy clears link storage and confirmation expires', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const values = new Map()
  const storage = { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value) }
  const { instance } = await controller({ storage })
  await instance.start()
  assert.equal(instance.getSnapshot().url, '')
  assert.equal(instance.getSnapshot().copied, true)
  const { instance: reloaded } = await controller({ storage })
  assert.equal(reloaded.getSnapshot().url, '')
  assert.equal(reloaded.getSnapshot().requestId, '')
  t.mock.timers.tick(3000)
  assert.equal(instance.getSnapshot().copied, false)
  assert.equal(instance.getSnapshot().message, '')
})

test('legacy completed links do not return on reload and blocked links can be dismissed', async () => {
  const storage = { getItem: () => JSON.stringify({ requestId: 'old', url: 'https://meet.google.com/abc-defg-hij' }), setItem: () => {} }
  const { instance } = await controller({ storage, copy: async () => { throw new Error('blocked') } })
  assert.equal(instance.getSnapshot().url, '')
  assert.equal(instance.getSnapshot().requestId, '')
  await instance.start()
  assert.ok(instance.getSnapshot().url)
  instance.dismiss()
  assert.equal(instance.getSnapshot().url, '')
  assert.equal(instance.getSnapshot().message, '')
})

test('nickname link is copied without an email or a Google permission', async () => {
  let copied
  const { instance } = await controller({
    create: async () => ({ url: 'https://g.co/meet/w-7a3f-92bc-e614-08d2' }),
    copy: async url => { copied = url },
  })
  await instance.start()
  assert.equal(copied, 'https://g.co/meet/w-7a3f-92bc-e614-08d2')
  assert.equal(instance.getSnapshot().copied, true)
})
