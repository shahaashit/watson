import test from 'node:test'
import assert from 'node:assert/strict'
import { renderJsx } from './renderJsx.js'
import { api } from '../src/api.js'

test('ClickUp offers browser connection and renewal without credential inputs', async () => {
  const html = await renderJsx('src/components/ClickUpConnect.jsx')
  assert.match(html, /Connect ClickUp/)
  assert.doesNotMatch(html, /textarea|password|token/)
  assert.match(await renderJsx('src/components/ClickUpConnect.jsx', { connected: true }), /Reconnect ClickUp/)
})

test('destination requires loaded Workspace, Space and List choices before saving', async () => {
  const html = await renderJsx('src/components/ClickUpDestination.jsx', {
    integration: { workspace_id: 'w', space_id: 's', create_list_id: 'l' },
  })
  for (const label of ['Workspace', 'Space', 'List']) assert.match(html, new RegExp(label))
  assert.equal((html.match(/<select[^>]*disabled=""/g) || []).length, 3)
  assert.match(html, /<button[^>]*disabled=""[^>]*>Save destination/)
  assert.doesNotMatch(html, /textarea|password|token/)
})

test('ClickUp API encodes path identifiers and saves only the local destination', async (t) => {
  const requests = []
  t.mock.method(globalThis, 'fetch', async (path, options) => {
    requests.push({ path, ...options })
    return { ok: true, json: async () => ({ status: 'pending' }) }
  })
  assert.equal(typeof api.connectClickup, 'function')
  await api.connectClickup()
  await api.clickupConnectStatus('session/id')
  await api.clickupWorkspaces()
  await api.clickupSpaces('work/space')
  await api.clickupLists('work/space', 'space/id')
  await api.saveClickupDestination({ workspace_id: 'w', space_id: 's', list_id: 'l' })
  assert.deepEqual(requests.map(({ path }) => path), [
    '/api/settings/integrations/clickup/connect',
    '/api/settings/integrations/clickup/connect/session%2Fid',
    '/api/settings/integrations/clickup/workspaces',
    '/api/settings/integrations/clickup/workspaces/work%2Fspace/spaces',
    '/api/settings/integrations/clickup/workspaces/work%2Fspace/spaces/space%2Fid/lists',
    '/api/settings/integrations/clickup/destination',
  ])
  assert.equal(requests[0].method, 'POST')
  assert.equal(requests[5].method, 'POST')
  assert.deepEqual(JSON.parse(requests[5].body), { workspace_id: 'w', space_id: 's', list_id: 'l' })
})
