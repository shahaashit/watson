import test from 'node:test'
import assert from 'node:assert/strict'

import { shouldDismissLayer } from '../src/useDismissibleLayer.js'

const inside = { id: 'inside' }
const outside = { id: 'outside' }
const root = { contains: (target) => target === inside }

test('an outside pointer interaction dismisses an open layer', () => {
  assert.equal(shouldDismissLayer(root, { type: 'pointerdown', target: outside }), true)
})

test('an interaction inside the layer is retained', () => {
  assert.equal(shouldDismissLayer(root, { type: 'pointerdown', target: inside }), false)
})

test('Escape dismisses while unrelated keys are retained', () => {
  assert.equal(shouldDismissLayer(root, { type: 'keydown', key: 'Escape' }), true)
  assert.equal(shouldDismissLayer(root, { type: 'keydown', key: 'Enter' }), false)
})
