import test from 'node:test'
import assert from 'node:assert/strict'
import { boardModeFromProfile, profileBoardPreference } from '../src/boardMode.js'

test('board mode defaults to a single priority list', () => {
  assert.equal(boardModeFromProfile(undefined), 'flat')
  assert.equal(boardModeFromProfile({ separate_work_by_status: false }), 'flat')
})

test('board mode preserves the explicit segregated profile choice', () => {
  assert.equal(boardModeFromProfile({ separate_work_by_status: true }), 'segregated')
  assert.deepEqual(profileBoardPreference('segregated'), { separate_work_by_status: true })
  assert.deepEqual(profileBoardPreference('flat'), { separate_work_by_status: false })
})
