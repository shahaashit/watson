import test from 'node:test'
import assert from 'node:assert/strict'
import { placementAround } from '../src/teamDrag.js'

test('hover placement moves cards in the pointer direction', () => {
  const items = [{ id: 1 }, { id: 2 }, { id: 3 }]

  assert.deepEqual(placementAround(items, 1, 2, 'next'), {
    state: 'next', afterId: 2,
  })
  assert.deepEqual(placementAround(items, 3, 2, 'next'), {
    state: 'next', beforeId: 2,
  })
})
