import test from 'node:test'
import assert from 'node:assert/strict'
import { changeBoardCardState, moveBoardCard } from '../src/boardOrder.js'

const card = (id, state) => ({ id, state, title: String(id) })

test('flat board reorders globally without changing local state', () => {
  const board = {
    mode: 'flat',
    items: [card(1, 'today'), card(2, 'waiting'), card(3, 'next')],
  }

  const result = moveBoardCard(board, 3, 'next', 1)

  assert.deepEqual(result.items.map(({ id }) => id), [3, 1, 2])
  assert.equal(result.items[0].state, 'next')
  assert.deepEqual(board.items.map(({ id }) => id), [1, 2, 3])
})

test('segregated board moves a card across sections before a target', () => {
  const board = {
    mode: 'segregated',
    items: [card(1, 'today'), card(2, 'next')],
    today: [card(1, 'today')],
    next: [card(2, 'next')],
    waiting: [card(3, 'waiting')],
    done: [],
  }

  const result = moveBoardCard(board, 2, 'today', 1)

  assert.deepEqual(result.today.map(({ id }) => id), [2, 1])
  assert.equal(result.today[0].state, 'today')
  assert.deepEqual(result.next, [])
})

test('segregated board appends into an empty section', () => {
  const board = {
    mode: 'segregated', items: [card(1, 'next')],
    today: [], next: [card(1, 'next')], waiting: [], done: [],
  }

  const result = moveBoardCard(board, 1, 'waiting')

  assert.deepEqual(result.waiting.map(({ id }) => id), [1])
  assert.equal(result.waiting[0].state, 'waiting')
})

test('explicit state changes remain available on a flat board', () => {
  const board = { mode: 'flat', items: [card(1, 'next'), card(2, 'waiting')] }

  const result = changeBoardCardState(board, 1, 'today')

  assert.deepEqual(result.items.map(({ id }) => id), [1, 2])
  assert.equal(result.items[0].state, 'today')
})

test('completing work removes it from a flat active board immediately', () => {
  const board = { mode: 'flat', items: [card(1, 'next'), card(2, 'waiting')] }

  const result = changeBoardCardState(board, 1, 'done')

  assert.deepEqual(result.items.map(({ id }) => id), [2])
})
