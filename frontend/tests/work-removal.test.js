import test from 'node:test'
import assert from 'node:assert/strict'
import { renderJsx } from './renderJsx.js'
import { api } from '../src/api.js'

test('removal shows structured API conflict guidance but hides unexpected failures', async (t) => {
  const { localWorkFailureMessage } = await import('../src/localWorkMutation.js')
  const guidance = 'Review task creation is already in progress. Wait for it to finish before removing work.'
  const fallback = 'Could not remove this from Watson. Please retry.'
  let status = 409
  let detail = guidance
  t.mock.method(globalThis, 'fetch', async () => ({ ok: false, status, statusText: 'Failure', json: async () => ({ detail }) }))
  const failure = async () => { try { await api.removeWork(7) } catch (error) { return error } }
  assert.equal(localWorkFailureMessage(await failure(), fallback), guidance)
  status = 500
  assert.equal(localWorkFailureMessage(await failure(), fallback), fallback)
  status = 409
  detail = { internal: 'unexpected structured error' }
  assert.equal(localWorkFailureMessage(await failure(), fallback), fallback)
  assert.equal(localWorkFailureMessage(new Error(guidance), fallback), fallback)
  assert.equal(localWorkFailureMessage(Object.assign(new Error(guidance), { status: 409 }), fallback), fallback)
})

test('local mutations hold cache reads, refresh on success and always release on failure', async () => {
  const { runLocalWorkMutation } = await import('../src/localWorkMutation.js')
  const events = []
  const monitor = { hold: () => { events.push('hold'); return () => events.push('release') }, refresh: () => events.push('refresh') }
  const detail = { id: 7, removed_at: '2026-01-01' }
  assert.deepEqual(await runLocalWorkMutation(async () => { events.push('post'); return { work_item: detail } }, monitor), detail)
  assert.deepEqual(events, ['hold', 'post', 'refresh', 'release'])
  events.length = 0
  await assert.rejects(runLocalWorkMutation(async () => { throw new Error('offline') }, monitor), /offline/)
  assert.deepEqual(events, ['hold', 'release'])
})

test('removal API uses only reversible local POSTs and a cancellable local list read', async (t) => {
  const calls = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url, ...options })
    return { ok: true, json: async () => ({ work_item: { id: 7, removed_at: null } }) }
  })
  const signal = new AbortController().signal
  await api.removedWork({ signal })
  await api.removeWork(7)
  await api.restoreWork(7)
  await api.removeWorkLink(7, 12)
  await api.restoreWorkLink(7, 12)
  assert.deepEqual(calls.map(call => [call.url, call.method || 'GET']), [
    ['/api/work-items/removed', 'GET'],
    ['/api/work-items/7/remove', 'POST'],
    ['/api/work-items/7/restore', 'POST'],
    ['/api/work-items/7/links/12/remove', 'POST'],
    ['/api/work-items/7/links/12/restore', 'POST'],
  ])
  assert.equal(calls[0].signal, signal)
})

test('detail removal controls retain access and replace removal with restore after removal', async () => {
  const active = await renderJsx('src/components/WorkRemovalControls.jsx')
  assert.match(active, /Work actions/)
  assert.match(active, /Remove from Watson/)
  const removed = await renderJsx('src/components/WorkRemovalControls.jsx', { removedAt: '2026-01-01', busy: true })
  assert.match(removed, /Removed from Watson/)
  assert.match(removed, /Restoring/)
  assert.match(removed, /previous state/)
  assert.doesNotMatch(removed, /return the work to your boards/)
  assert.match(removed, /disabled=""/)
  assert.doesNotMatch(removed, /<summary|Remove from Watson<\/button>/)
})

test('confirmation explicitly describes local removal and offers cancellation', async () => {
  const html = await renderJsx('src/components/LocalRemovalConfirm.jsx', { title: 'Remove sample work?', actionLabel: 'Remove from Watson' })
  assert.match(html, /<dialog/)
  assert.match(html, /Only removes from Watson\. ClickUp tasks and GitLab MRs are unchanged\./)
  assert.match(html, />Cancel<\/button>/)
  assert.match(html, />Remove from Watson<\/button>/)
  assert.doesNotMatch(html, /permanent|Delete/)
})

test('MR unlink controls are absent; same-repository labels and old restore identifiers survive', async () => {
  const links = [
    { id: 11, source_type: 'gitlab_mr', external_id: '1!10', label: 'First change', url: 'https://gitlab.example.com/sample/app/-/merge_requests/10' },
    { id: 12, source_type: 'gitlab_mr', external_id: '1!11', label: 'Second change', url: 'https://gitlab.example.com/sample/app/-/merge_requests/11' },
    { id: 13, source_type: 'clickup', external_id: 'sample123' },
  ]
  const html = await renderJsx('src/components/LinkedWork.jsx', { links, onUnlink: () => {}, onRestore: () => {}, removedLinks: [
    { ...links[0], id: 14, external_id: '1!12', label: 'Unlinked change' },
  ] })
  assert.doesNotMatch(html, /Unlink from Watson<\/button>/)
  assert.match(html, /merge_requests\/10/)
  assert.match(html, /merge_requests\/11/)
  assert.match(html, /First change \(1!10\)/)
  assert.match(html, /Second change \(1!11\)/)
  assert.match(html, /Unlinked change/)
  assert.match(html, /Restore link/)
  assert.doesNotMatch(html, /Unlink sample123/)
})

test('removed work list renders restore, loading, busy and error states', async () => {
  const entry = { id: 7, title: 'Sample removed work', removed_at: '2026-01-01' }
  const html = await renderJsx('src/components/RemovedWorkList.jsx', { items: [entry], busyId: 7 })
  assert.match(html, /Sample removed work/)
  assert.match(html, /href="\/work\/7"/)
  assert.match(html, /disabled=""[^>]*>Restoring/)
  assert.match(await renderJsx('src/components/RemovedWorkList.jsx', { loading: true }), /Loading removed work/)
  const error = await renderJsx('src/components/RemovedWorkList.jsx', { error: 'Could not load removed work.' })
  assert.match(error, /role="alert"/)
  assert.match(error, />Retry<\/button>/)
  assert.doesNotMatch(error, /No removed work/)
})

test('removed work is offered only in the Data and Backup settings view', async () => {
  const data = await renderJsx('src/components/SyncDataSettings.jsx', { mode: 'data' })
  const sync = await renderJsx('src/components/SyncDataSettings.jsx', { mode: 'sync' })
  assert.match(data, /Removed work/)
  assert.doesNotMatch(sync, /Removed work/)
})
