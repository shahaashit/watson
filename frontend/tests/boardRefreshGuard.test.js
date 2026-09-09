import test from 'node:test'
import assert from 'node:assert/strict'
import { attachBoardRefreshGuard } from '../src/boardRefreshGuard.js'

test('native drag keeps refresh paused through pointercancel until dragend', () => {
  const target = new EventTarget()
  target.closest = () => true
  const windowTarget = new EventTarget()
  let held = 0
  const detach = attachBoardRefreshGuard(target, windowTarget, () => { held++; return () => held-- })
  target.dispatchEvent(new Event('pointerdown'))
  target.dispatchEvent(new Event('dragstart'))
  target.dispatchEvent(new Event('pointercancel'))
  assert.equal(held, 1)
  target.dispatchEvent(new Event('dragend'))
  assert.equal(held, 0)
  target.dispatchEvent(new Event('pointerdown'))
  windowTarget.dispatchEvent(new Event('blur'))
  assert.equal(held, 0)
  detach()
})
