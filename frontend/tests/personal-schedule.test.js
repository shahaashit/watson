import test from 'node:test'
import assert from 'node:assert/strict'
import { renderJsx } from './renderJsx.js'

const now = new Date(2026, 8, 9, 12, 0)
const iso = (hour) => new Date(2026, 8, 9, hour, 0).toISOString()
const meetings = [
  { event_id: 'past', title: 'Finished', start_at: iso(9), end_at: iso(10) },
  { event_id: 'later', title: 'Later meeting', start_at: iso(15), end_at: iso(16) },
  { event_id: 'next', title: 'Next meeting', start_at: iso(13), end_at: iso(14) },
  { event_id: 'all-day', title: 'All day reminder', all_day: true, start_at: '2026-09-09', end_at: '2026-09-10' },
  { event_id: 'tomorrow', title: 'Tomorrow meeting', start_at: new Date(2026, 8, 10, 9).toISOString() },
]

test('personal calendar retains finished meetings today and highlights the next timed meeting', async () => {
  const html = await renderJsx('src/components/ScheduleStrip.jsx', { personal: true, meetings, now })
  assert.match(html, />Finished</)
  assert.doesNotMatch(html, /Tomorrow meeting/)
  assert.match(html, /is-next[^>]*[\s\S]*?Next up[\s\S]*?Next meeting/)
  assert.ok(html.indexOf('Next meeting') < html.indexOf('Later meeting'))
  assert.match(html, /All day reminder/)
})

test('ongoing meetings take priority and an ended day has an honest empty state', async () => {
  const ongoing = { event_id: 'ongoing', title: 'In progress', start_at: iso(11), end_at: iso(13) }
  const html = await renderJsx('src/components/ScheduleStrip.jsx', { personal: true, now, meetings: [...meetings, ongoing] })
  assert.match(html, /is-next[^>]*[\s\S]*?Now[\s\S]*?In progress/)
  const ended = await renderJsx('src/components/ScheduleStrip.jsx', { personal: true, now, meetings: [meetings[0]] })
  assert.match(ended, />Finished</)
  assert.doesNotMatch(ended, /is-next|No more meetings/)
  assert.match(await renderJsx('src/components/ScheduleStrip.jsx', { personal: true, now, meetings: [] }), /No meetings today/)
})

test('default schedule retains existing meeting selection', async () => {
  const html = await renderJsx('src/components/ScheduleStrip.jsx', { meetings, now })
  assert.match(html, />Finished</)
  assert.doesNotMatch(html, /Next up|is-next/)
})
