import test from 'node:test'
import assert from 'node:assert/strict'

import { reorderIds } from '../src/workOrder.js'

test('moves one id before another without duplication', () => {
  assert.deepEqual(reorderIds([1, 2, 3], 3, 1), [3, 1, 2])
})

test('moves one id after another', () => {
  assert.deepEqual(reorderIds([1, 2, 3], 1, undefined, 3), [2, 3, 1])
})

test('rejects missing and conflicting placement targets', () => {
  assert.throws(() => reorderIds([1, 2, 3], 4, 1), /moved id/i)
  assert.throws(() => reorderIds([1, 2, 3], 3, 4), /before id/i)
  assert.throws(() => reorderIds([1, 2, 3], 3, 1, 2), /both before and after/i)
})

test('rejects duplicate input IDs before it can create an ambiguous ordering', () => {
  assert.throws(() => reorderIds([1, 2, 2, 3], 3, 1), /unique/i)
})

test('rejects moving an id relative to itself', () => {
  assert.throws(() => reorderIds([1, 2, 3], 2, 2), /itself/i)
  assert.throws(() => reorderIds([1, 2, 3], 2, undefined, 2), /itself/i)
})

test('rejects a duplicated placement target even when both targets match', () => {
  assert.throws(() => reorderIds([1, 2, 3], 3, 1, 1), /both before and after/i)
})
