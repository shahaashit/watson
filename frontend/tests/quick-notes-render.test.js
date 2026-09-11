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

test('A closed notes panel claims no column on either board', async () => {
  for (const view of ['src/views/MyWork.jsx', 'src/views/Team.jsx']) {
    const html = await withStorage('false', () => renderJsx(view))
    assert.match(html, /class="board-split"/, view)
    assert.doesNotMatch(html, /with-notes|notes-panel/, view)
    assert.match(html, /class="notes-toggle"[^>]*aria-pressed="false"/, view)
  }
})

test('An open notes panel splits the board grid on either board', async () => {
  for (const view of ['src/views/MyWork.jsx', 'src/views/Team.jsx']) {
    const html = await withStorage('true', () => renderJsx(view))
    assert.match(html, /class="board-split with-notes"/, view)
    assert.match(html, /<aside class="notes-panel"[^>]*aria-label="Quick notes"/, view)
    assert.match(html, /class="notes-toggle active"[^>]*aria-pressed="true"/, view)
    assert.match(html, /Jot something down/, view)
  }
})

test('The team board labels your own lane as yours', async () => {
  const lane = { name: 'Taylor', person: { id: 1, is_self: true }, items: [] }
  const html = await renderJsx('src/components/PersonLane.jsx', { lane })
  assert.match(html, />You</)
  assert.doesNotMatch(html, />Team member</)
})
