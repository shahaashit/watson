import test from 'node:test'
import assert from 'node:assert/strict'
import { renderJsx } from './renderJsx.js'

test('profile offers valid timezone choices and one onboarding submit action', async () => {
  const html = await renderJsx('src/components/ProfileSettings.jsx', { compact: true })
  assert.match(html, /<select[^>]*>[\s\S]*value="Asia\/Kolkata" selected=""/)
  assert.match(html, /IST/)
  assert.match(html, /alex@example.com/)
  assert.match(html, /Continue to Integrations/)
  assert.doesNotMatch(html, /Save profile/)
})

test('settings preserves an existing timezone outside the suggested choices', async () => {
  const html = await renderJsx('src/components/ProfileSettings.jsx', { profile: { timezone: 'Australia/Perth' } })
  assert.match(html, /value="Australia\/Perth" selected=""/)
  assert.match(html, /Save profile/)
})
