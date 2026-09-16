import test from 'node:test'
import assert from 'node:assert/strict'
import { api } from '../src/api.js'

test('Add MR posts only the URL to the current local work item', async t => {
  const calls = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url, options })
    return { ok: true, json: async () => ({ work_item: { id: 7 } }) }
  })
  await api.addGitlabMr(7, 'https://gitlab.example.com/sample/app/-/merge_requests/12')
  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, '/api/work-items/7/gitlab-mrs')
  assert.equal(calls[0].options.method, 'POST')
  assert.deepEqual(JSON.parse(calls[0].options.body), { url: 'https://gitlab.example.com/sample/app/-/merge_requests/12' })
})

test('MR form only clears after confirmed success and never submits blank input', async () => {
  const { submitLinkedMr } = await import('../src/linkedMrForm.js')
  const submitted = []
  let cleared = 0
  const clear = () => { cleared++ }
  await submitLinkedMr('   ', async value => submitted.push(value), clear)
  assert.deepEqual(submitted, [])
  await submitLinkedMr(' https://gitlab.example.com/sample/app/-/merge_requests/12 ', async value => { submitted.push(value); return false }, clear)
  assert.equal(cleared, 0)
  await submitLinkedMr('https://gitlab.example.com/sample/app/-/merge_requests/12', async () => true, clear)
  assert.equal(cleared, 1)
  assert.equal(submitted[0], 'https://gitlab.example.com/sample/app/-/merge_requests/12')
})
