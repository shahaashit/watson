import { useEffect, useRef, useState } from 'react'
import useDismissibleLayer from '../useDismissibleLayer.js'
import { personalSchedule } from '../personalSchedule.js'
import {
  cleanMeetingDescription,
  formatMeetingTimeRange,
  meetingClickState,
  meetingProvider,
  summarizeAttendees,
} from '../meetingDetails.js'

function timeLabel(iso) {
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

function MeetingDetails({ meeting, position, onMouseEnter, onMouseLeave }) {
  const description = cleanMeetingDescription(meeting.description)
  const attendees = Array.isArray(meeting.attendees) ? meeting.attendees : []
  return (
    <div id="schedule-meeting-details" className="schedule-event-popover" role="dialog" aria-label={`${meeting.title || 'Untitled meeting'} details`} style={position} onMouseEnter={onMouseEnter} onMouseLeave={onMouseLeave}>
      <div className="schedule-popover-heading">
        <strong>{meeting.title || 'Untitled meeting'}</strong>
        <span>{formatMeetingTimeRange(meeting)}</span>
      </div>
      {(meeting.meet_link || meeting.html_link) && <div className="schedule-popover-actions">
        {meeting.meet_link && <a className="primary" href={meeting.meet_link} target="_blank" rel="noreferrer">Join {meetingProvider(meeting.meet_link)}</a>}
        {meeting.html_link && <a href={meeting.html_link} target="_blank" rel="noreferrer">Open calendar</a>}
      </div>}
      {meeting.location && <div className="schedule-popover-row"><span>Location</span><p>{meeting.location}</p></div>}
      {meeting.organizer && <div className="schedule-popover-row"><span>Organizer</span><p>{meeting.organizer}</p></div>}
      {attendees.length > 0 && <div className="schedule-popover-row"><span>Attendees</span><p>{summarizeAttendees(attendees)}</p></div>}
      {description && <p className="schedule-popover-description">{description}</p>}
    </div>
  )
}

export default function ScheduleStrip({ meetings = [], loading, error, personal = false, now }) {
  const [clock, setClock] = useState(() => new Date())
  useEffect(() => {
    if (!personal || now) return undefined
    const timer = setInterval(() => setClock(new Date()), 60000)
    return () => clearInterval(timer)
  }, [personal, now])
  const currentTime = now || clock
  const selection = personal ? personalSchedule(meetings, currentTime) : { meetings }
  const visibleMeetings = selection.meetings
  const [activeMeeting, setActiveMeeting] = useState(null)
  const [popoverPosition, setPopoverPosition] = useState({ left: 12 })
  const showTimer = useRef(null)
  const hideTimer = useRef(null)
  const focusOpenedId = useRef(null)
  const rootRef = useDismissibleLayer({ open: Boolean(activeMeeting), onDismiss: () => { focusOpenedId.current = null; setActiveMeeting(null) } })

  const cancelTimers = () => {
    if (showTimer.current) clearTimeout(showTimer.current)
    if (hideTimer.current) clearTimeout(hideTimer.current)
    showTimer.current = null
    hideTimer.current = null
  }
  useEffect(() => cancelTimers, [])

  const reveal = (meeting, trigger) => {
    cancelTimers()
    const rootRect = rootRef.current?.getBoundingClientRect()
    const triggerRect = trigger?.getBoundingClientRect()
    if (rootRect && triggerRect) {
      const cardWidth = Math.min(370, Math.max(280, rootRect.width - 24))
      const preferred = triggerRect.left - rootRect.left
      setPopoverPosition({ left: Math.max(12, Math.min(preferred, rootRect.width - cardWidth - 12)) })
    }
    setActiveMeeting(meeting)
  }
  const revealSoon = (meeting, trigger) => {
    cancelTimers()
    showTimer.current = setTimeout(() => reveal(meeting, trigger), 140)
  }
  const hideSoon = () => {
    cancelTimers()
    hideTimer.current = setTimeout(() => { focusOpenedId.current = null; setActiveMeeting(null) }, 160)
  }
  const toggleMeeting = (meeting, trigger) => {
    const next = meetingClickState(activeMeeting?.event_id || null, focusOpenedId.current, meeting.event_id)
    focusOpenedId.current = next.focusOpenedId
    if (!next.activeId) { cancelTimers(); setActiveMeeting(null); return }
    if (activeMeeting?.event_id !== next.activeId) reveal(meeting, trigger)
  }

  return (
    <section
      ref={rootRef}
      className={`schedule-strip${personal ? ' personal-schedule' : ''}${activeMeeting ? ' has-open-event' : ''}`}
      aria-label="Today’s schedule"
      onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) { focusOpenedId.current = null; setActiveMeeting(null) } }}
      onPointerDown={(event) => { if (!event.target.closest?.('.schedule-event-trigger, .schedule-event-popover')) { focusOpenedId.current = null; setActiveMeeting(null) } }}
    >
      <div className="schedule-heading"><h2>{personal ? 'My calendar' : 'Schedule'}</h2>{!personal && <span>Today</span>}</div>
      {loading && <p className="schedule-status">Loading schedule…</p>}
      {error && <p className="schedule-status">Schedule is unavailable; your work lists are still up to date.</p>}
      {!loading && !error && (!visibleMeetings.length ? <p className="schedule-status">{personal ? 'No meetings today.' : 'No meetings on the calendar.'}</p> : (
        <div className="schedule-events">
          {(personal ? visibleMeetings : visibleMeetings.slice(0, 4)).map((meeting) => <button
            type="button"
            key={meeting.event_id}
            className={`schedule-event-trigger${personal && meeting.event_id === selection.nextId ? ' is-next' : ''}`}
            aria-haspopup="dialog"
            aria-expanded={activeMeeting?.event_id === meeting.event_id}
            aria-controls={activeMeeting?.event_id === meeting.event_id ? 'schedule-meeting-details' : undefined}
            onMouseEnter={(event) => revealSoon(meeting, event.currentTarget)}
            onMouseLeave={hideSoon}
            onFocus={(event) => {
              if (activeMeeting?.event_id !== meeting.event_id) {
                focusOpenedId.current = meeting.event_id
                reveal(meeting, event.currentTarget)
              }
            }}
            onClick={(event) => toggleMeeting(meeting, event.currentTarget)}
          >
            {personal && meeting.event_id === selection.nextId && <strong className="schedule-next-label">{new Date(meeting.start_at) <= currentTime ? 'Now' : 'Next up'}</strong>}
            <time>{meeting.all_day ? 'All day' : timeLabel(meeting.start_at)}</time><span>{meeting.title || 'Untitled meeting'}</span>
          </button>)}
          {!personal && visibleMeetings.length > 4 && <span className="schedule-more">+{visibleMeetings.length - 4} more</span>}
        </div>
      ))}
      {activeMeeting && <MeetingDetails meeting={activeMeeting} position={popoverPosition} onMouseEnter={cancelTimers} onMouseLeave={hideSoon} />}
    </section>
  )
}
