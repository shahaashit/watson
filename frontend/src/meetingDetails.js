export function meetingProvider(url) {
  if (/meet\.google\.com/i.test(url || '')) return 'Google Meet'
  if (/zoom\.us/i.test(url || '')) return 'Zoom'
  if (/teams\.microsoft\.com/i.test(url || '')) return 'Microsoft Teams'
  if (/webex\.com/i.test(url || '')) return 'Webex'
  if (/meet\.jit\.si/i.test(url || '')) return 'Jitsi'
  return 'call'
}

export function formatMeetingTimeRange(meeting, locale, timeZone) {
  if (meeting.all_day) return 'All day'
  const start = new Date(meeting.start_at)
  if (Number.isNaN(start.getTime())) return ''
  const options = { hour: '2-digit', minute: '2-digit' }
  if (timeZone) options.timeZone = timeZone
  const startLabel = start.toLocaleTimeString(locale, options)
  const end = new Date(meeting.end_at)
  if (!meeting.end_at || Number.isNaN(end.getTime())) return startLabel
  return `${startLabel}–${end.toLocaleTimeString(locale, options)}`
}

export function cleanMeetingDescription(value) {
  const compact = String(value || '')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;/gi, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  return compact.length > 260 ? `${compact.slice(0, 260)}…` : compact
}

export function summarizeAttendees(attendees = [], limit = 4) {
  const visible = attendees.slice(0, limit).join(', ')
  const remaining = attendees.length - limit
  return remaining > 0 ? `${visible} +${remaining}` : visible
}

export function meetingClickState(activeId, focusOpenedId, clickedId) {
  if (focusOpenedId === clickedId) return { activeId: clickedId, focusOpenedId: null }
  return { activeId: activeId === clickedId ? null : clickedId, focusOpenedId: null }
}
