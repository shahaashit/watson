import test from 'node:test'
import assert from 'node:assert/strict'
import { renderJsx } from './renderJsx.js'

test('GitLab repository picker identifies selected projects and manual-link exception', async () => {
  const markup = await renderJsx('src/components/GitLabProjectPicker.jsx', {
    projects: [
      { id: 41, name: 'frontend-service', path: 'cm/frontend-service' },
      { id: 52, name: 'backend-service', path: 'cm/go/backend-service' },
      { id: 63, name: 'other', path: 'other/repo' },
    ],
    selectedIds: [41, 52],
  })

  assert.match(markup, /Tracked repositories/)
  assert.match(markup, /Search GitLab/)
  assert.match(markup, /Manually imported MR links remain available/)
  assert.match(markup, /<input[^>]*checked=""[^>]*value="41"/)
  assert.match(markup, /<input[^>]*checked=""[^>]*value="52"/)
  assert.match(markup, /value="63"/)
  assert.doesNotMatch(markup, /value="63" checked=""/)
  assert.match(markup, /cm\/frontend-service/)
  assert.match(markup, /cm\/go\/backend-service/)
})
