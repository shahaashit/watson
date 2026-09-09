import test from 'node:test'
import assert from 'node:assert/strict'
import { renderJsx } from './renderJsx.js'

test('Team work cards render compact repository names with full-path context', async () => {
  const markup = await renderJsx('src/components/WorkCard.jsx', {
    item: {
      id: 7,
      title: 'Multi-repo work',
      state: 'next',
      repositories: ['cm/frontend-service', 'cm/go/backend-service'],
    },
    index: 0,
    total: 1,
  })

  assert.match(markup, /class="work-card-repositories"/)
  assert.match(markup, /title="cm\/frontend-service"[^>]*>frontend-service</)
  assert.match(markup, /title="cm\/go\/backend-service"[^>]*>backend-service</)
})

test('Team lanes make each whole card draggable without permanent icon controls', async () => {
  const markup = await renderJsx('src/components/PersonLane.jsx', {
    lane: {
      name: 'Morgan',
      person: { id: 4, display_name: 'Morgan' },
      items: [
        { id: 1, title: 'First', state: 'next' },
        { id: 2, title: 'Second', state: 'next' },
        { id: 3, title: 'Third', state: 'next' },
      ],
    },
    mode: 'flat',
  })

  assert.match(markup, /class="work-card-main" data-drag-surface="true"/)
  assert.match(markup, /data-work-item-id="1"/)
  assert.doesNotMatch(markup, /draggable="true"|work-drag-handle|title="Move Up"|title="Move Down"/)
  assert.match(markup, />First</)
  assert.match(markup, />Second</)
  assert.match(markup, />Third</)
})

test('work cards do not expose the legacy local state', async () => {
  const markup = await renderJsx('src/components/WorkCard.jsx', {
    item: { id: 7, title: 'State-free work', state: 'next' },
    index: 0,
    total: 1,
  })

  assert.doesNotMatch(markup, />Next</)
  assert.doesNotMatch(markup, /work-card-state/)
  assert.doesNotMatch(markup, /<select|another state/)
})

test('work cards expose the complete task title as hover context', async () => {
  const title = '[Video module] Media Player Sound and Viewability Optimisation'
  const markup = await renderJsx('src/components/WorkCard.jsx', {
    item: { id: 8, title, state: 'next' },
    index: 0,
    total: 1,
  })

  assert.match(markup, new RegExp(`class="work-card-title" title="${title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}"`))
})

test('Team lanes stay flat even when a legacy segregated mode is returned', async () => {
  const markup = await renderJsx('src/components/PersonLane.jsx', {
    lane: {
      name: 'Morgan',
      person: { id: 4, display_name: 'Morgan' },
      items: [
        { id: 1, title: 'First', state: 'today' },
        { id: 2, title: 'Second', state: 'next' },
      ],
    },
    mode: 'segregated',
  })

  assert.match(markup, />First</)
  assert.match(markup, />Second</)
  assert.doesNotMatch(markup, /person-lane-state|>Today|>Next/)
})
