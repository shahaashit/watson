import assert from 'node:assert/strict'
import test from 'node:test'
import { renderJsx } from './renderJsx.js'

test('MR anchors display repository names and keep their original destinations', async () => {
  const url = 'https://gitlab.example.com/platform/service-api/-/merge_requests/42'
  const markup = await renderJsx('src/components/LinkedWork.jsx', {
    links: [{ source_type: 'gitlab_mr', external_id: '1!42', url, label: 'Long task title' }],
  })
  assert.ok(markup.includes(`href="${url}"`))
  assert.match(markup, />service-api<span/)
  assert.doesNotMatch(markup, />Long task title</)
})

test('ClickUp links remain clickable when their cached URL is missing', async () => {
  for (const url of ['', null, undefined]) {
    const markup = await renderJsx('src/components/LinkedWork.jsx', {
      links: [{ source_type: 'clickup', external_id: '86d3example', url }],
    })
    assert.match(markup, /href="https:\/\/app.clickup.com\/t\/86d3example"/)
    assert.match(markup, />ClickUp task<span/)
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

test('linked ClickUp work displays task kind instead of a raw URL', async () => {
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

  assert.match(markup, /href="https:\/\/app.clickup.com\/t\/86d3example"/)
  assert.match(markup, />ClickUp task<span/)
  assert.doesNotMatch(markup, />ClickUp 86d3example</)
})

test('ClickUp aliases dedupe by canonical task ID, preserve distinct IDs and count visible links', async () => {
  const links = [
    { id: 1, source_type: 'clickup', url: 'https://app.clickup.com/t/sampleA?view=board', task_kind: 'original' },
    { id: 2, source_type: 'clickup', url: 'https://app.clickup.com/t/sampleA/#comments' },
    { id: 3, source_type: 'clickup', external_id: 'sampleA' },
    { id: 4, source_type: 'clickup', url: 'https://app.clickup.com/t/sampleB', task_kind: 'review' },
    { id: 5, source_type: 'clickup', url: 'https://app.clickup.com/t/sampleC', task_kind: 'unknown' },
  ]
  const html = await renderJsx('src/components/LinkedWork.jsx', { links })
  assert.equal((html.match(/<a /g) || []).length, 3)
  assert.match(html, /Linked work<\/h2><span>3<\/span>/)
  assert.match(html, />Original task<span/)
  assert.match(html, />Review task<span/)
  assert.match(html, />ClickUp task<span/)
})

test('noncanonical ClickUp URLs are not accidentally merged', async () => {
  const { groupLinkedWork } = await import('../src/linkedWorkGroups.js')
  const links = [
    { id: 1, source_type: 'clickup', url: 'https://example.com/t/sampleA' },
    { id: 2, source_type: 'clickup', url: 'https://app.clickup.com/t/sampleA/other' },
    { id: 3, source_type: 'clickup', url: 'https://app.clickup.com/t/sampleA' },
    { id: 4, source_type: 'gitlab_mr', url: 'https://gitlab.example.com/app/-/merge_requests/1' },
  ]
  assert.deepEqual(groupLinkedWork(links).flatMap(group => group.links).map(link => link.id), [1, 2, 3, 4])
})

test('Add MR is available by the GitLab heading even with no links, but absent on removed work', async () => {
  const active = await renderJsx('src/components/LinkedWork.jsx', { links: [], onAddMr: async () => true })
  assert.match(active, />GitLab MRs<\/h3>/)
  assert.match(active, /aria-expanded="false"[^>]*>\+ Add MR/)
  assert.doesNotMatch(active, /type="url"/)
  const removed = await renderJsx('src/components/LinkedWork.jsx', { onAddMr: async () => true, removed: true })
  assert.doesNotMatch(removed, /Add MR|type="url"/)
})
