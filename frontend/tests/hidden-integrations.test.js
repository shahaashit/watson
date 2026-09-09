import test from 'node:test'
import assert from 'node:assert/strict'
import { renderJsx } from './renderJsx.js'

test('unreleased integration stays absent even when configured', async () => {
  const markup = await renderJsx('src/components/IntegrationSettings.jsx', {
    integrations: { flock: { configured: true, profile_dir: '/tmp/profile' } },
  })
  assert.doesNotMatch(markup, /Flock|Browser profile directory/)
  assert.match(markup, /Google Calendar/)
})

test('unreleased integration failures do not inflate attention count', async () => {
  const markup = await renderJsx('src/components/IntegrationHealth.jsx', {
    sources: [{ source: 'flock', status: 'degraded' }],
  })
  assert.match(markup, /Integrations cached/)
  assert.doesNotMatch(markup, /need attention|Flock/)
})
