import test from 'node:test'
import assert from 'node:assert/strict'
import * as notesPanel from '../src/notesPanel.js'
import { renderJsx } from './renderJsx.js'

async function withStorage(value, run) {
  const previous = globalThis.localStorage
  globalThis.localStorage = { getItem: () => value, setItem: () => {} }
  try { return await run() } finally {
    if (previous === undefined) delete globalThis.localStorage
    else globalThis.localStorage = previous
  }
}

test('The notes toggle preference survives a page refresh', () => {
  const values = new Map()
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  }

  assert.equal(notesPanel.readNotesOpen(storage), false)
  notesPanel.writeNotesOpen(true, storage)
  assert.equal(notesPanel.readNotesOpen(storage), true)
  notesPanel.writeNotesOpen(false, storage)
  assert.equal(notesPanel.readNotesOpen(storage), false)
})

test('A closed notes panel claims no column on the board', async () => {
  const html = await withStorage('false', () => renderJsx('src/views/Team.jsx'))
  assert.match(html, /class="board-split"/)
  assert.doesNotMatch(html, /with-notes|notes-panel/)
  assert.match(html, /class="notes-toggle"[^>]*aria-pressed="false"/)
})

test('An open notes panel splits the board grid', async () => {
  const html = await withStorage('true', () => renderJsx('src/views/Team.jsx'))
  assert.match(html, /class="board-split with-notes"/)
  assert.match(html, /<aside class="notes-panel"[^>]*aria-label="Quick notes"/)
  assert.match(html, /class="notes-toggle active"[^>]*aria-pressed="true"/)
  assert.match(html, /Jot something down/)
})

test('The team board labels your own lane as yours', async () => {
  const lane = { name: 'Taylor', person: { id: 1, is_self: true }, items: [] }
  const html = await renderJsx('src/components/PersonLane.jsx', { lane })
  assert.match(html, />You</)
  assert.doesNotMatch(html, />Team member</)
})
