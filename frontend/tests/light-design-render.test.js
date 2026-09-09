import test from 'node:test'
import assert from 'node:assert/strict'
import { renderJsx } from './renderJsx.js'

test('person lanes identify names with initials, including single names and extra whitespace', async () => {
  for (const [name, initials] of [['Alex Morgan', 'AM'], ['  Priya   Shah  ', 'PS'], ['Sam', 'S']]) {
    const html = await renderJsx('src/components/PersonLane.jsx', {
      lane: { name, person: { id: 1 }, items: [] },
    })
    assert.match(html, new RegExp(`class="person-avatar" aria-hidden="true">${initials}</span>`))
    assert.match(html, /No work here yet/)
  }
})

test('unassigned lanes use a neutral marker rather than invented person initials', async () => {
  const html = await renderJsx('src/components/PersonLane.jsx', { lane: { name: 'Unassigned', items: [] } })
  assert.match(html, /class="person-avatar" aria-hidden="true">–<\/span>/)
})
