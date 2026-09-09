import test from 'node:test'
import assert from 'node:assert/strict'
import { renderJsx } from './renderJsx.js'

test('personal rows keep owned reviews and separate ClickUp links from opening local work', async () => {
  const html = await renderJsx('src/components/PersonalTaskList.jsx', { items: [
    { id: 1, title: 'Review - Release checks', state: 'next', clickup_url: 'https://app.clickup.com/t/example1' },
    { id: 2, title: 'Write delivery note', state: 'today' },
  ] })
  assert.match(html, /Review - Release checks/)
  assert.match(html, /Write delivery note/)
  assert.match(html, /<button[^>]*aria-label="Open Review - Release checks"[^>]*>[\s\S]*?<\/button>/)
  assert.match(html, /<a[^>]*href="https:\/\/app.clickup.com\/t\/example1"[^>]*target="_blank"[^>]*rel="noreferrer"/)
  for (const button of html.matchAll(/<button[^>]*class="personal-task-open"[^>]*>[\s\S]*?<\/button>/g)) {
    assert.doesNotMatch(button[0], /<a\b/)
  }
  assert.equal((html.match(/draggable="true"/g) || []).length, 2)
  assert.match(html, /<button[^>]*disabled=""[^>]*aria-label="Move Review - Release checks up"/)
  assert.match(html, /<button[^>]*disabled=""[^>]*aria-label="Move Write delivery note down"/)
  assert.doesNotMatch(html, /Updated|GitLab repositories|work-card-description/)
})

test('personal rows omit unsafe source URLs and explain an empty task list', async () => {
  const html = await renderJsx('src/components/PersonalTaskList.jsx', { items: [
    { id: 1, title: 'Local task', clickup_url: 'javascript:alert(1)' },
    { id: 2, title: 'Unexpected source', clickup_url: 'https://example.com/task' },
  ] })
  assert.doesNotMatch(html, /<a\b/)
  assert.match(await renderJsx('src/components/PersonalTaskList.jsx'), /No active tasks/)
})

test('My Work renders only personal day, tasks and capture surfaces', async () => {
  const html = await renderJsx('src/views/MyWork.jsx')
  assert.match(html, /My day/)
  assert.match(html, /My tasks/)
  assert.match(html, /Capture a thought or ask Watson/)
  assert.doesNotMatch(html, /integration-health|work-inbox|watson-suggestions|my-work-support-column/)
})
