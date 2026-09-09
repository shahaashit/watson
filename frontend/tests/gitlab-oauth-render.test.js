import test from 'node:test'
import assert from 'node:assert/strict'
import { renderJsx } from './renderJsx.js'

test('GitLab login and renewal have explicit browser sign-in actions', async () => {
  const login = await renderJsx('src/components/GitLabConnect.jsx')
  assert.match(login, /Connect GitLab/)
  assert.doesNotMatch(login, /textarea|password|token/)
  const renewal = await renderJsx('src/components/GitLabConnect.jsx', { connected: true })
  assert.match(renewal, /Reconnect GitLab/)
})

test('reconnect notice is absent before a failed connection is detected', async () => {
  assert.equal(await renderJsx('src/components/ConnectionNotice.jsx'), '')
})
