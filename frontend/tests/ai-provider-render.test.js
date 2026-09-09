import test from 'node:test'
import assert from 'node:assert/strict'
import { renderJsx } from './renderJsx.js'

const card = async (integration) => {
  const html = await renderJsx('src/components/IntegrationSettings.jsx', {
    integrations: { anthropic: integration },
  })
  return html.match(/<article[\s\S]*?<\/article>/)[0]
}

test('new AI setup offers FastRouter, a key link and collapsed advanced defaults', async () => {
  const html = await card({ configured: false, base_url: '', model: '' })
  assert.match(html, /<select[^>]*>[\s\S]*FastRouter/)
  assert.match(html, /href="https:\/\/fastrouter.ai\/"/)
  assert.match(html, /<details><summary>Advanced settings<\/summary>/)
  assert.match(html, /value="https:\/\/go.fastrouter.ai"/)
  assert.match(html, /value="claude-sonnet-4-6"/)
  const advanced = html.match(/<details>[\s\S]*?<\/details>/)[0]
  assert.doesNotMatch(advanced, /textarea/)
  assert.match(html, /<textarea/)
})

test('existing custom endpoint and model remain usable without relabelling as FastRouter', async () => {
  const html = await card({ configured: true, credential_present: true,
    base_url: 'https://gateway.example.com', model: 'existing-model' })
  assert.match(html, /value="https:\/\/gateway.example.com"/)
  assert.match(html, /value="existing-model"/)
  assert.match(html, /<option value="custom"[^>]*selected="">/)
  assert.match(html, /Replace API key/)
})

test('configured direct Anthropic connection preserves its empty base URL', async () => {
  const html = await card({ configured: true, credential_present: true, base_url: '', model: 'existing-model' })
  assert.match(html, /Base URL<input[^>]*value=""/)
  assert.match(html, /<option value="custom"[^>]*selected="">/)
})
