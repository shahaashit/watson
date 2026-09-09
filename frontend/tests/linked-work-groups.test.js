import assert from 'node:assert/strict'
import test from 'node:test'
import { renderJsx } from './renderJsx.js'

test('ClickUp links remain clickable when their cached URL is missing', async () => {
  for (const url of ['', null, undefined]) {
    const markup = await renderJsx('src/components/LinkedWork.jsx', {
      links: [{ source_type: 'clickup', external_id: '86d3example', url }],
    })
    assert.match(markup, /href="https:\/\/app.clickup.com\/t\/86d3example"/)
    assert.match(markup, />https:\/\/app.clickup.com\/t\/86d3example</)
  }
})

test('missing or malformed IDs do not produce invented ClickUp links', async () => {
  for (const external_id of ['', undefined, '../bad', 'javascript:alert(1)']) {
    const markup = await renderJsx('src/components/LinkedWork.jsx', {
      links: [{ source_type: 'clickup', external_id }],
    })
    assert.doesNotMatch(markup, /<a /)
  }
})

test('groups merge requests from multiple repositories into one GitLab section', async () => {
  const module = await import('../src/linkedWorkGroups.js').catch(() => ({}))
  assert.equal(typeof module.groupLinkedWork, 'function')
  const groups = module.groupLinkedWork([
    { id: 1, source_type: 'clickup', external_id: 'cu-1' },
    { id: 2, source_type: 'gitlab_mr', external_id: '156!6406' },
    { id: 3, source_type: 'gitlab_mr', external_id: '14020!4587' },
  ])

  assert.deepEqual(
    groups.map((group) => ({
      key: group.key,
      label: group.label,
      externalIds: group.links.map((link) => link.external_id),
    })),
    [
      { key: 'clickup', label: 'ClickUp', externalIds: ['cu-1'] },
      {
        key: 'gitlab_mr',
        label: 'GitLab MRs',
        externalIds: ['156!6406', '14020!4587'],
      },
    ],
  )
})

test('linked ClickUp work displays its URL instead of an opaque task id', async () => {
  const url = 'https://app.clickup.com/t/86d3example'
  const markup = await renderJsx('src/components/LinkedWork.jsx', {
    links: [{
      id: 1,
      source_type: 'clickup',
      external_id: '86d3example',
      url,
      label: '',
    }],
  })

  assert.match(markup, new RegExp(`>${url}<`))
  assert.doesNotMatch(markup, />ClickUp 86d3example</)
})
