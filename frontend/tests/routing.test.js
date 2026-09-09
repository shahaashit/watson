import test from 'node:test'
import assert from 'node:assert/strict'

import { parseRoute } from '../src/routing.js'

test('parses primary and work detail routes', () => {
  assert.deepEqual(parseRoute('/'), { view: 'my-work' })
  assert.deepEqual(parseRoute('/my-work'), { view: 'my-work' })
  assert.deepEqual(parseRoute('/team'), { view: 'team' })
  assert.deepEqual(parseRoute('/work/42'), { view: 'work-detail', workItemId: 42 })
  assert.deepEqual(parseRoute('/log'), { view: 'log' })
  assert.deepEqual(parseRoute('/settings/integrations'), { view: 'settings', section: 'integrations' })
  assert.deepEqual(parseRoute('/onboarding'), { view: 'onboarding' })
})

test('falls back to My Work for unsupported paths and invalid work ids', () => {
  assert.deepEqual(parseRoute('/missing'), { view: 'my-work' })
  assert.deepEqual(parseRoute('/work/not-a-number'), { view: 'my-work' })
  assert.deepEqual(parseRoute('/work/0'), { view: 'my-work' })
})

test('normalizes trailing slashes and rejects non-canonical detail routes', () => {
  for (const [path, expected] of [
    ['/my-work///', { view: 'my-work' }],
    ['/work/42///', { view: 'work-detail', workItemId: 42 }],
    ['/settings/integrations//', { view: 'settings', section: 'integrations' }],
    ['/settings/unknown', { view: 'my-work' }],
    ['/work/0', { view: 'my-work' }],
    ['/work/042', { view: 'my-work' }],
    ['/work/\u0664\u0662', { view: 'my-work' }],
    ['/work/1234567890123456', { view: 'my-work' }],
  ]) {
    assert.deepEqual(parseRoute(path), expected)
  }
})

test('navigate updates history and route subscribers receive navigation and back events', async () => {
  const originalWindow = global.window
  const originalEvent = global.Event
  const events = new EventTarget()
  const historyCalls = []
  global.window = {
    location: { pathname: '/' },
    history: {
      pushState(_state, _title, path) {
        historyCalls.push(path)
        global.window.location.pathname = path
      },
    },
    addEventListener: events.addEventListener.bind(events),
    removeEventListener: events.removeEventListener.bind(events),
    dispatchEvent: events.dispatchEvent.bind(events),
  }
  global.Event = Event

  try {
    const { navigate, subscribeRoute } = await import('../src/routing.js')
    const received = []
    const unsubscribe = subscribeRoute((route) => received.push(route))

    navigate('/team')
    global.window.location.pathname = '/log'
    global.window.dispatchEvent(new Event('popstate'))
    unsubscribe()
    navigate('/onboarding')

    assert.deepEqual(historyCalls, ['/team', '/onboarding'])
    assert.deepEqual(received, [{ view: 'team' }, { view: 'log' }])
  } finally {
    global.window = originalWindow
    global.Event = originalEvent
  }
})
