import test from 'node:test'
import assert from 'node:assert/strict'
import { renderJsx } from './renderJsx.js'

test('unreleased integration stays absent even when configured', async () => {
  const markup = await renderJsx('src/components/IntegrationSettings.jsx', {
    integrations: { flock: { configured: true, profile_dir: '/tmp/profile' } },
  })
  assert.doesNotMatch(markup, /Flock|Browser profile directory/)
  assert.match(markup, /Google/)
  assert.match(markup, /Meet nickname links/)
  assert.doesNotMatch(markup, /Google Calendar/)
})

test('unreleased integration failures do not inflate attention count', async () => {
  const markup = await renderJsx('src/components/IntegrationHealth.jsx', {
    sources: [{ source: 'flock', status: 'degraded' }],
  })
  assert.match(markup, /Integrations cached/)
  assert.doesNotMatch(markup, /need attention|Flock/)
})

test('Google card distinguishes failed Calendar connection from available Meet links', async () => {
  const markup = await renderJsx('src/components/IntegrationSettings.jsx', {
    integrations: { 'google-calendar': { configured: true, credential_present: true, health: { status: 'failed' } } },
  })
  assert.match(markup, /Calendar: Needs attention/)
  assert.match(markup, /Meet links are ready/)
  assert.match(markup, /Reconnect Google to restore Calendar sync/)
  assert.doesNotMatch(markup, /Application setup is ready\. Use Connect Google/)
})
