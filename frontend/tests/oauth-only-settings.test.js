import test from 'node:test'
import assert from 'node:assert/strict'
import { renderJsx } from './renderJsx.js'

test('OAuth providers offer browser connection without manual credential entry', async () => {
  const html = await renderJsx('src/components/IntegrationSettings.jsx', { integrations: {
    gitlab: { oauth_available: true, oauth_connected: true, configured: true },
    clickup: { oauth_available: true },
    'google-calendar': { credential_present: true },
  } })
  for (const source of ['gitlab', 'clickup', 'google-calendar']) {
    const card = html.match(new RegExp(`<article[^>]*data-provider="${source}"[\\s\\S]*?</article>`))[0]
    assert.doesNotMatch(card, /<textarea|Manual connection settings|Import \.env|Save &amp; connect|Save changes/)
    assert.match(card, /(?:Connect|Reconnect) (?:GitLab|ClickUp|Google)/)
  }
  assert.doesNotMatch(html, /Import \.env/)
})

test('missing OAuth app setup explains the setup file without exposing credential fields', async () => {
  const html = await renderJsx('src/components/IntegrationSettings.jsx')
  for (const source of ['gitlab', 'clickup', 'google-calendar']) {
    const card = html.match(new RegExp(`<article[^>]*data-provider="${source}"[\\s\\S]*?</article>`))[0]
    assert.match(card, /setup file/)
    assert.doesNotMatch(card, /<textarea|Manual connection settings/)
  }
})
