import test from 'node:test'
import assert from 'node:assert/strict'

import {
  cleanMeetingDescription,
  formatMeetingTimeRange,
  meetingClickState,
  meetingProvider,
  summarizeAttendees,
} from '../src/meetingDetails.js'

test('meeting providers produce familiar direct-join labels', () => {
  for (const [url, expected] of [
    ['https://meet.google.com/abc-defg-hij', 'Google Meet'],
    ['https://acme.zoom.us/j/123', 'Zoom'],
    ['https://teams.microsoft.com/l/meetup-join/123', 'Microsoft Teams'],
    ['https://example.test/call/123', 'call'],
    ['', 'call'],
  ]) assert.equal(meetingProvider(url), expected)
})

test('meeting time ranges handle timed and all-day events', () => {
  assert.equal(formatMeetingTimeRange({ all_day: true }), 'All day')
  assert.equal(formatMeetingTimeRange({
    start_at: '2026-09-02T10:00:00Z',
    end_at: '2026-09-02T10:45:00Z',
  }, 'en-GB', 'UTC'), '10:00–10:45')
  assert.equal(formatMeetingTimeRange({ start_at: 'not-a-date' }, 'en-GB', 'UTC'), '')
})

test('calendar descriptions are made compact and safe to scan', () => {
  assert.equal(cleanMeetingDescription('<p>Agenda:</p>  Review&nbsp; launch\n readiness'), 'Agenda: Review launch readiness')
  assert.equal(cleanMeetingDescription('x'.repeat(300)).length, 261)
  assert.ok(cleanMeetingDescription('x'.repeat(300)).endsWith('…'))
})

test('attendee summaries remain compact without hiding the remaining count', () => {
  assert.equal(summarizeAttendees(['a@example.com', 'b@example.com', 'c@example.com']), 'a@example.com, b@example.com, c@example.com')
  assert.equal(summarizeAttendees(['a', 'b', 'c', 'd', 'e'], 3), 'a, b, c +2')
})

test('the click that follows focus keeps a newly opened meeting visible', () => {
  assert.deepEqual(meetingClickState('evt-1', 'evt-1', 'evt-1'), { activeId: 'evt-1', focusOpenedId: null })
  assert.deepEqual(meetingClickState('evt-1', null, 'evt-1'), { activeId: null, focusOpenedId: null })
  assert.deepEqual(meetingClickState('evt-1', null, 'evt-2'), { activeId: 'evt-2', focusOpenedId: null })
})
