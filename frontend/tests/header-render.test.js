import test from 'node:test'
import assert from 'node:assert/strict'
import { renderJsx } from './renderJsx.js'

test('the header renders one balanced action cluster in the approved order', async () => {
  const originalWindow = global.window
  global.window = { location: { pathname: '/' } }

  try {
    const markup = await renderJsx('src/App.jsx')
    const addWork = markup.indexOf('Add Work')
    const search = markup.indexOf('aria-label="Search Watson"')
    const overflow = markup.indexOf('aria-label="Open utility menu"')

    assert.ok(addWork >= 0, 'Add Work remains directly available')
    assert.ok(search > addWork, 'Search follows Add Work')
    assert.ok(overflow > search, 'the overflow menu follows Search')
    assert.match(markup, />Search</)
    assert.match(markup, /⌘K/)
    assert.doesNotMatch(markup, /aria-label="Open Settings menu"/)
  } finally {
    global.window = originalWindow
  }
})
