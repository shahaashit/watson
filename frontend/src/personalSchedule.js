// Keep the whole local day visible, including meetings that already ended.
export function personalSchedule(meetings, now = new Date()) {
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
  const dayEnd = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1).getTime()
  const dayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const remaining = meetings.filter(meeting => {
    if (meeting.all_day) {
      const start = String(meeting.start_at || '').slice(0, 10)
      const end = String(meeting.end_at || '').slice(0, 10)
      return start && start <= today && (end ? end > today : start === today)
    }
    const start = new Date(meeting.start_at).getTime()
    const end = new Date(meeting.end_at || meeting.start_at).getTime()
    return Number.isFinite(start) && Number.isFinite(end) && end >= dayStart && start < dayEnd
  }).sort((a, b) => Number(Boolean(a.all_day)) - Number(Boolean(b.all_day))
    || new Date(a.start_at).getTime() - new Date(b.start_at).getTime())
  return { meetings: remaining, nextId: remaining.find(meeting => !meeting.all_day && new Date(meeting.end_at || meeting.start_at).getTime() > now.getTime())?.event_id }
}
