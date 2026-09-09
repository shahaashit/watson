import test from 'node:test'
import assert from 'node:assert/strict'
import * as teamLanes from '../src/teamLanes.js'
import { renderJsx } from './renderJsx.js'

test('Me mode preference survives a page refresh', () => {
  assert.equal(typeof teamLanes.readMeMode, 'function')
  assert.equal(typeof teamLanes.writeMeMode, 'function')
  const values = new Map()
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  }

  assert.equal(teamLanes.readMeMode(storage), false)
  teamLanes.writeMeMode(true, storage)
  assert.equal(teamLanes.readMeMode(storage), true)
  teamLanes.writeMeMode(false, storage)
  assert.equal(teamLanes.readMeMode(storage), false)
})

test('Team renders the persisted Me mode as an accessible switch', async () => {
  const previousStorage = globalThis.localStorage
  globalThis.localStorage = { getItem: () => 'true', setItem: () => {} }
  try {
    const html = await renderJsx('src/views/Team.jsx')
    assert.match(html, /role="switch"/)
    assert.match(html, /aria-checked="true"/)
    assert.match(html, />Me mode</)
  } finally {
    if (previousStorage === undefined) delete globalThis.localStorage
    else globalThis.localStorage = previousStorage
  }
})
