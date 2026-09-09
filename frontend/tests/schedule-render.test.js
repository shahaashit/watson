import test from 'node:test'
import assert from 'node:assert/strict'
import { renderJsx } from './renderJsx.js'

test('calendar entries render as detail-card triggers instead of calendar links', async () => {
  const markup = await renderJsx('src/components/ScheduleStrip.jsx', {
    meetings: [{
      event_id: 'evt-1', title: 'Playback check-in',
      start_at: '2026-09-03T13:00:00+05:30', end_at: '2026-09-03T13:30:00+05:30',
      meet_link: 'https://meet.google.com/abc-defg-hij',
      html_link: 'https://calendar.google.com/calendar/event?eid=evt-1',
    }],
  })

  assert.match(markup, /<button[^>]+class="schedule-event-trigger"/)
  assert.match(markup, /aria-haspopup="dialog"/)
  assert.match(markup, />Playback check-in</)
  assert.doesNotMatch(markup, /<a[^>]+class="schedule-event"/)
})
