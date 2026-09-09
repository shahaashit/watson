// Today — the day-at-a-glance dashboard. Layout D from the mockup: a horizontal
// timeline spine at top, unified "pulling on you" inbox on the left (Flock
// mentions + Watson-drafted pending actions interleaved by urgency), MR
// reference table + reminders on the right, capture bar at the bottom.
//
// All data comes from /api/today. Approve/Reject on pending actions calls the
// real actions endpoints; Reply/Open buttons on Flock items open the chat's
// web URL. Snooze/Dismiss are UI-only for now (no backend endpoint yet).
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api.js'
import Markdown from '../components/Markdown.jsx'


// User-selectable auto-sync intervals. "manual" disables the periodic
// timer entirely — the focus-triggered stale-check still fires so a Mac
// coming out of sleep still catches up, it's just not on a fixed cadence.
const SYNC_INTERVALS = [
  { key: '5m',     label: '5m',     ms: 5 * 60_000 },
  { key: '30m',    label: '30m',    ms: 30 * 60_000 },
  { key: '1h',     label: '1h',     ms: 60 * 60_000 },
  { key: 'manual', label: 'Manual', ms: null },
]
const DEFAULT_INTERVAL = '30m'
const INTERVAL_STORAGE_KEY = 'watson.today.sync_interval'
// Staleness threshold for the focus-triggered catch-up. Aligned with the
// server-side scheduler tick (30 min) + margin; independent of the
// user's interval choice above so "manual" mode still resyncs after wake.
const STALENESS_MS = 35 * 60 * 1000


// Browser-notification config. Watson lives in a tab that stays open, so
// we use the Web Notifications API instead of shelling out to macOS —
// zero deps, cross-platform, macOS still routes them through the native
// notification center. Fired IDs live in localStorage, scoped by date so
// yesterday's fires don't suppress today's.
const NOTIF_LEAD_MINUTES = 5
const NOTIF_CHECK_INTERVAL_MS = 30_000
const NOTIF_STORAGE_KEY = 'watson.notifications.fired'
const NOTIF_SOUND_KEY = 'watson.notifications.sound'


// Two-tone WebAudio chime — synthesized so we don't ship a binary asset.
// The Notifications API's own audio is unreliable on Chrome desktop (it
// depends on OS notification-center settings), so we play our own on top.
// AudioContext needs an existing user gesture; the tab having been focused
// once is enough on Chrome. Failures are swallowed — a missed chime is not
// worth breaking the notification for.
function playChime() {
  try {
    if (localStorage.getItem(NOTIF_SOUND_KEY) === 'off') return
    const AC = window.AudioContext || window.webkitAudioContext
    if (!AC) return
    const ctx = playChime._ctx || (playChime._ctx = new AC())
    if (ctx.state === 'suspended') ctx.resume()
    const now = ctx.currentTime
    const tones = [880, 1174.66]  // A5 → D6
    tones.forEach((freq, i) => {
      const t0 = now + i * 0.16
      const gain = ctx.createGain()
      gain.gain.setValueAtTime(0.0001, t0)
      gain.gain.exponentialRampToValueAtTime(0.22, t0 + 0.02)
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.35)
      gain.connect(ctx.destination)
      const osc = ctx.createOscillator()
      osc.type = 'sine'
      osc.frequency.setValueAtTime(freq, t0)
      osc.connect(gain)
      osc.start(t0)
      osc.stop(t0 + 0.4)
    })
  } catch { /* audio blocked — silent notification is fine */ }
}


function loadFiredIds() {
  try {
    const raw = JSON.parse(localStorage.getItem(NOTIF_STORAGE_KEY) || '{}')
    const today = new Date().toISOString().slice(0, 10)
    if (raw.date !== today || !Array.isArray(raw.ids)) {
      return { date: today, ids: [] }
    }
    return raw
  } catch {
    return { date: new Date().toISOString().slice(0, 10), ids: [] }
  }
}


function saveFiredIds(state) {
  try { localStorage.setItem(NOTIF_STORAGE_KEY, JSON.stringify(state)) } catch { /* private mode */ }
}


function useNotifications(meetings, reminders) {
  const stateRef = useRef(null)
  if (!stateRef.current) stateRef.current = loadFiredIds()

  // Request permission once. If denied, the hook silently becomes a no-op.
  useEffect(() => {
    if (typeof window === 'undefined' || !('Notification' in window)) return
    if (Notification.permission === 'default') Notification.requestPermission()
  }, [])

  useEffect(() => {
    if (typeof window === 'undefined' || !('Notification' in window)) return

    const check = () => {
      if (Notification.permission !== 'granted') return
      const fired = stateRef.current
      const has = (k) => fired.ids.includes(k)
      const mark = (k) => {
        if (!fired.ids.includes(k)) {
          fired.ids.push(k)
          saveFiredIds(fired)
        }
      }
      const now = Date.now()

      // ─── meetings ───────────────────────────────────────
      for (const m of meetings || []) {
        if (m.all_day) continue
        const start = new Date(m.start_at).getTime()
        if (isNaN(start)) continue
        const url = m.meet_link || m.html_link || ''
        const title = m.title || '(untitled meeting)'
        const msToStart = start - now
        const leadMs = NOTIF_LEAD_MINUTES * 60_000

        // T-5: within the last 60s of the lead window (i.e. between 5m
        // and 4m before start). One-minute window guarantees a tick catches
        // it without double-firing.
        const leadKey = `meet-lead:${m.event_id}`
        if (!has(leadKey) && msToStart <= leadMs && msToStart > (leadMs - 60_000)) {
          playChime()
          const n = new Notification(`Meeting in ${NOTIF_LEAD_MINUTES} min`, {
            body: `${title}${url ? '\n' + url : ''}`,
            tag: leadKey,
            icon: '/favicon.ico',
          })
          if (url) n.onclick = () => { window.focus(); window.open(url, '_blank') }
          else     n.onclick = () => window.focus()
          mark(leadKey)
        }

        // T-0: within the first 60s after start.
        const startKey = `meet-start:${m.event_id}`
        if (!has(startKey) && msToStart <= 0 && msToStart > -60_000) {
          playChime()
          const n = new Notification('Meeting starting now', {
            body: `${title}${url ? '\n' + url : ''}`,
            tag: startKey,
            icon: '/favicon.ico',
          })
          if (url) n.onclick = () => { window.focus(); window.open(url, '_blank') }
          else     n.onclick = () => window.focus()
          mark(startKey)
        }
      }

      // ─── reminders ──────────────────────────────────────
      // Fire once when due_at first crosses "now" (within the last 60s).
      // A reminder that came due hours ago while the tab was closed is
      // still visible in the Today card — no need to blast a notification
      // for something the user can see themselves.
      for (const r of reminders || []) {
        if (r.status !== 'pending') continue
        const due = new Date(r.due_at).getTime()
        if (isNaN(due)) continue
        const msToDue = due - now
        if (msToDue > 0 || msToDue <= -60_000) continue
        const key = `rem:${r.id}`
        if (has(key)) continue
        playChime()
        const n = new Notification('Reminder', {
          body: r.text || '(no text)',
          tag: key,
          icon: '/favicon.ico',
        })
        n.onclick = () => window.focus()
        mark(key)
      }
    }

    check()
    const id = setInterval(check, NOTIF_CHECK_INTERVAL_MS)
    return () => clearInterval(id)
  }, [meetings, reminders])
}


// Manage the bottom-fade signal on a scroll container plus a "+N more" chip
// that tells the user exactly how many items are hidden below. Both go away
// when the user has scrolled to the end (`.at-bottom`) or the content fits
// without overflow (`.no-overflow`). Returns `{ ref, hiddenBelow, showChip }`
// — ref to attach to the scroll element, hiddenBelow for the chip label.
function useScrollFade(getItemHeight) {
  const ref = useRef(null)
  const [state, setState] = useState({ hiddenBelow: 0, showChip: false })
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const update = () => {
      const canScroll = el.scrollHeight > el.clientHeight + 2
      const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 4
      el.classList.toggle('no-overflow', !canScroll)
      el.classList.toggle('at-bottom', canScroll && atBottom)
      // Approximate hidden-below count for the chip. Prefer measuring the
      // first child's actual height so grouped MR rows / tall inbox items
      // don't produce a garbage count.
      let itemH = getItemHeight?.() ?? 0
      if (!itemH && el.firstElementChild) itemH = el.firstElementChild.getBoundingClientRect().height + 8
      const hiddenPx = Math.max(0, el.scrollHeight - el.scrollTop - el.clientHeight)
      const hiddenBelow = itemH > 0 ? Math.round(hiddenPx / itemH) : 0
      setState({ hiddenBelow, showChip: canScroll && !atBottom })
    }
    update()
    el.addEventListener('scroll', update, { passive: true })
    const ro = new ResizeObserver(update)
    ro.observe(el)
    // Content changes (React re-renders inserting/removing items) don't
    // fire ResizeObserver on the container itself unless its own box
    // resizes; observe the first child as a proxy for content changes.
    if (el.firstElementChild) ro.observe(el.firstElementChild)
    return () => { el.removeEventListener('scroll', update); ro.disconnect() }
  })   // no dep array — re-runs each render so we re-observe on child swaps
  return { ref, ...state }
}


export default function Today({ onChanged }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [captureText, setCaptureText] = useState('')
  // `syncing` is shared state between the manual Sync-now button, the auto
  // focus-triggered sync, and the header indicator — anything that fires a
  // sync should set this so the user sees a single unified "Syncing…" pill
  // on the header instead of state that only lives inside one component.
  const [syncing, setSyncing] = useState(false)
  const [syncTrigger, setSyncTrigger] = useState('')   // "manual" | "auto"
  const [intervalKey, setIntervalKey] = useState(() => {
    try { return localStorage.getItem(INTERVAL_STORAGE_KEY) || DEFAULT_INTERVAL }
    catch { return DEFAULT_INTERVAL }
  })
  useEffect(() => {
    try { localStorage.setItem(INTERVAL_STORAGE_KEY, intervalKey) } catch { /* private mode */ }
  }, [intervalKey])
  // Ref-guard against a rapid double-fire (mount + visibilitychange racing,
  // or two visibility flips mid-sync) trying to launch concurrent /api/sync
  // calls. The backend has its own lock, but skipping the round-trip is
  // cheaper than round-tripping to get "skipped: already_running".
  const syncInFlightRef = useRef(false)

  const refresh = useCallback(() => {
    api.today()
      .then((d) => { setData(d); setError(null) })
      .catch((e) => setError(e.message))
  }, [])

  const runSync = useCallback(async (trigger = 'manual') => {
    if (syncInFlightRef.current) return
    syncInFlightRef.current = true
    setSyncing(true); setSyncTrigger(trigger)
    try {
      const result = await api.syncAll()   // blocks ~50s
      // If the pipeline was already running (scheduler mid-tick), wait for
      // THAT run to finish before releasing our indicator — otherwise the
      // user would see "done" while stale data is still on screen.
      if (result?.skipped === 'already_running') {
        while (true) {
          await new Promise((r) => setTimeout(r, 2000))
          const s = await api.syncStatus()
          if (!s.running) break
        }
      }
      refresh()
    } catch { /* silent — next tick retries */ }
    finally {
      syncInFlightRef.current = false
      setSyncing(false); setSyncTrigger('')
    }
  }, [refresh])

  const maybeAutoSync = useCallback((lastSyncAt) => {
    if (!lastSyncAt) return
    const age = Date.now() - new Date(lastSyncAt).getTime()
    if (age > STALENESS_MS) runSync('auto')
  }, [runSync])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 60_000)
    const onDirty = () => refresh()
    // When the tab comes back into view, re-check staleness. Handles the
    // "Mac was asleep overnight, opened Watson in the morning" case without
    // waiting for the next 60s poll or forcing the user to click Sync-now.
    const onVisibility = () => {
      if (document.visibilityState === 'visible') refresh()
    }
    window.addEventListener('watson:today-dirty', onDirty)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      clearInterval(id)
      window.removeEventListener('watson:today-dirty', onDirty)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [refresh])

  // Whenever a fresh /api/today response comes in, check its last_sync_at
  // and auto-fire a sync if the data is stale. Debounced via syncInFlightRef.
  useEffect(() => {
    maybeAutoSync(data?.last_sync_at)
  }, [data?.last_sync_at, maybeAutoSync])

  // Browser notifications for meetings + reminders. Must be called before
  // any conditional return so React's hook order stays consistent — safe
  // to pass in-flight data (the hook handles empty arrays).
  useNotifications(data?.meetings || [], data?.reminders_due || [])

  // Fixed-interval auto-sync. Only runs when the tab is visible — no point
  // polling while the user is off in another app. "manual" mode skips this
  // entirely; the focus-triggered staleness check above still fires.
  useEffect(() => {
    const cfg = SYNC_INTERVALS.find((i) => i.key === intervalKey)
    if (!cfg || !cfg.ms) return
    const tick = () => {
      if (document.visibilityState === 'visible') runSync('auto')
    }
    const id = setInterval(tick, cfg.ms)
    return () => clearInterval(id)
  }, [intervalKey, runSync])

  const bumpChanged = useCallback(() => {
    refresh()
    onChanged?.()
    window.dispatchEvent(new Event('watson:today-dirty'))
  }, [refresh, onChanged])

  if (!data && !error) return <div className="today-page"><p className="empty">Loading today…</p></div>
  if (error) return <div className="today-page"><p className="error-text">Could not load today: {error}</p></div>

  const meetings = data.meetings || []
  const flock = data.flock || []
  const pending = data.pending_actions || []
  // Feed the whole review queue into the MR table — it scrolls internally
  // when the list exceeds the card height, so there's no reason to pre-cap.
  // Prefer stale-first ordering when any stale MRs exist, otherwise all opened.
  const mrs = data.stale_mrs?.length ? data.stale_mrs : (data.mrs || [])
  const reminders = data.reminders_due || []

  const pullingCount = flock.length + pending.length
  const gitlabBase = extractGitlabBase(data.mrs || [])

  return (
    <div className="today-page">
      <div className="today-head">
        <div className="today-head-left">
          <h1 className="today-h1">Today</h1>
          <div className="today-date">{formatDateLong(new Date())}</div>
        </div>
        <div className="today-stats">
          <div className="today-stat"><span className="today-stat-num attn">{pullingCount}</span><span className="today-stat-lbl">Pull on you</span></div>
          <div className="today-stat"><span className="today-stat-num">{meetings.length}</span><span className="today-stat-lbl">Meetings</span></div>
          <div className="today-stat"><span className="today-stat-num">{flock.length}</span><span className="today-stat-lbl">Flock</span></div>
          <div className="today-stat"><span className="today-stat-num">{(data.mrs || []).length}</span><span className="today-stat-lbl">MRs waiting</span></div>
          <div className="today-stat"><span className="today-stat-num">{pending.length}</span><span className="today-stat-lbl">Pending</span></div>
          <SyncControl
            syncing={syncing}
            trigger={syncTrigger}
            lastSyncAt={data.last_sync_at}
            intervalKey={intervalKey}
            onIntervalChange={setIntervalKey}
            onSyncClick={() => runSync('manual')}
          />
        </div>
      </div>

      <DayTimeline meetings={meetings} />

      <div className="today-grid">
        <div className="today-main">
          <AttentionInbox
            flock={flock}
            pending={pending}
            onChanged={bumpChanged}
            onCapture={(seed) => setCaptureText(seed)}
          />
        </div>
        <div className="today-side">
          <MRTable mrs={mrs} totalMRs={(data.mrs || []).length} gitlabBase={gitlabBase} />
          <ReminderList items={reminders} onChanged={bumpChanged} />
        </div>
      </div>

      <CaptureBar
        value={captureText}
        onChange={setCaptureText}
        onCaptured={bumpChanged}
      />
    </div>
  )
}


function extractGitlabBase(mrs) {
  // Grab origin from any MR url — the GitLab dashboard sits at /dashboard/…
  // on the same host. If we have no MRs yet, fall back to null so the UI hides
  // the link rather than sending you to a broken URL.
  const url = mrs.find((m) => m.url)?.url
  if (!url) return null
  try {
    const u = new URL(url)
    return `${u.protocol}//${u.host}`
  } catch { return null }
}


// ─── Day timeline ─────────────────────────────────────────────────────────
// Track spans 08:00 → 20:00 (12h). Meetings positioned by minutes-since-08:00.
// Meetings outside that window get clamped rather than dropped.

const TRACK_START_H = 8
const TRACK_END_H = 20
const TRACK_MINUTES = (TRACK_END_H - TRACK_START_H) * 60

function DayTimeline({ meetings: todaysMeetings }) {
  const hours = useMemo(() => {
    const out = []
    for (let h = TRACK_START_H; h < TRACK_END_H; h++) out.push(h)
    return out
  }, [])

  // Date navigation. `offset` is signed days from today (0 = today, -1 =
  // yesterday, +1 = tomorrow). For today we reuse the already-fetched
  // meetings; for any other date we JIT-fetch from /api/meetings which
  // hits Google Calendar live (cache is today-only by design).
  const [offset, setOffset] = useState(0)
  const [remoteMeetings, setRemoteMeetings] = useState(null)
  const [loading, setLoading] = useState(false)
  const [fetchError, setFetchError] = useState(null)
  const isToday = offset === 0
  const currentDate = useMemo(() => addDays(new Date(), offset), [offset])

  useEffect(() => {
    if (isToday) {
      setRemoteMeetings(null)
      setFetchError(null)
      return
    }
    let cancelled = false
    setLoading(true)
    setFetchError(null)
    api.meetingsForDate(toYMD(currentDate))
      .then((r) => { if (!cancelled) { setRemoteMeetings(r.meetings || []); if (r.error) setFetchError(r.error) } })
      .catch((e) => { if (!cancelled) setFetchError(String(e)) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [offset, isToday])

  const meetings = isToday ? todaysMeetings : (remoteMeetings || [])
  const now = new Date()
  // now-line only meaningful for today
  const nowPct = isToday ? clampPct(minutesSinceTrackStart(now) / TRACK_MINUTES * 100) : null

  const blocks = useMemo(() => {
    const raw = meetings
      .map((m) => {
        const startMin = timeIsoToMinutesInDay(m.start_at)
        const endMin = timeIsoToMinutesInDay(m.end_at) ?? startMin + 30
        if (startMin == null) return null
        const relStart = startMin - TRACK_START_H * 60
        const relEnd = endMin - TRACK_START_H * 60
        const left = clampPct((relStart / TRACK_MINUTES) * 100)
        const width = clampPct(((relEnd - relStart) / TRACK_MINUTES) * 100)
        const isNow = isToday && now.getTime() >= isoToMs(m.start_at) && now.getTime() <= isoToMs(m.end_at || m.start_at)
        return { m, left, width, isNow, startMin, endMin }
      })
      .filter(Boolean)
      .sort((a, b) => a.startMin - b.startMin)

    // Sweep-line lane assignment: two events overlap iff one starts before the
    // other ends. For each event, drop it into the lowest-index lane whose
    // most-recent event has already ended; otherwise open a new lane. This is
    // what Google Calendar does — overlapping events stack vertically inside
    // the same time slot instead of hiding each other.
    const laneEndMins = []   // per lane, end time of latest placed event
    for (const b of raw) {
      let placed = false
      for (let i = 0; i < laneEndMins.length; i++) {
        if (b.startMin >= laneEndMins[i]) {
          laneEndMins[i] = b.endMin
          b.lane = i
          placed = true
          break
        }
      }
      if (!placed) {
        b.lane = laneEndMins.length
        laneEndMins.push(b.endMin)
      }
    }
    const maxLanes = Math.max(1, laneEndMins.length)
    return raw.map((b) => ({ ...b, maxLanes }))
  }, [meetings, now, isToday])

  // Focus-time gaps between meetings (only within the track window).
  const focusGaps = useMemo(() => {
    if (blocks.length === 0) return []
    const gaps = []
    let cursor = TRACK_START_H * 60
    for (const b of blocks) {
      if (b.startMin > cursor + 15) {
        gaps.push({ start: cursor, end: b.startMin })
      }
      cursor = Math.max(cursor, b.endMin)
    }
    if (TRACK_END_H * 60 - cursor > 15) {
      gaps.push({ start: cursor, end: TRACK_END_H * 60 })
    }
    return gaps.map((g) => ({
      left: clampPct(((g.start - TRACK_START_H * 60) / TRACK_MINUTES) * 100),
      width: clampPct(((g.end - g.start) / TRACK_MINUTES) * 100),
      label: formatGap(g.end - g.start),
    }))
  }, [blocks])

  const focusTotal = focusGaps.reduce((s, g) => s + (g.width / 100) * TRACK_MINUTES, 0)

  return (
    <div className="day-timeline">
      <div className="day-timeline-head">
        <div className="day-timeline-title">
          <div className="day-timeline-nav">
            <button className="day-nav-btn" onClick={() => setOffset(offset - 1)} aria-label="Previous day" title="Previous day">‹</button>
            <span className="day-nav-label">
              {isToday ? 'Today' : formatDayLabel(currentDate)}
              {!isToday && (
                <button className="day-nav-jump" onClick={() => setOffset(0)} title="Jump back to today">today</button>
              )}
            </span>
            <button className="day-nav-btn" onClick={() => setOffset(offset + 1)} aria-label="Next day" title="Next day">›</button>
          </div>
          <span className="day-timeline-subtitle">
            · {meetings.length} meeting{meetings.length === 1 ? '' : 's'} · {formatMinutes(focusTotal)} focus
          </span>
        </div>
        <div className="day-timeline-now">
          {isToday ? `now: ${formatHM(now)}` : (loading ? 'loading…' : '')}
        </div>
      </div>
      <div
        className="day-timeline-track"
        style={
          // Grow the track when there are overlapping lanes so each block
          // stays comfortably readable. Single-lane keeps the compact
          // 88px default; each additional lane adds ~34px.
          (blocks[0]?.maxLanes || 1) > 1
            ? { height: `${54 + (blocks[0].maxLanes) * 34}px` }
            : undefined
        }
      >
        <div className="day-timeline-hours">
          {hours.map((h) => (
            <div key={h} className="day-timeline-hour">{h}</div>
          ))}
        </div>
        {focusGaps.map((g, i) => (
          <div key={`gap-${i}`} className="day-timeline-focus" style={{ left: `${g.left}%`, width: `${g.width}%` }}>
            {g.width > 6 ? g.label : ''}
          </div>
        ))}
        {blocks.map((b) => (
          <TimelineBlock
            key={b.m.event_id}
            meeting={b.m}
            left={b.left}
            width={b.width}
            isNow={b.isNow}
            lane={b.lane}
            maxLanes={b.maxLanes}
          />
        ))}
        {nowPct != null && (
          <div className="day-timeline-now-line" style={{ left: `${nowPct}%` }} />
        )}
      </div>
      <div className="day-timeline-status">
        {loading
          ? 'Loading…'
          : fetchError
          ? `Couldn't load calendar: ${fetchError}`
          : meetings.length === 0
          ? (isToday ? 'No meetings on the calendar today.' : 'No meetings on this day.')
          : ' '}
      </div>
    </div>
  )
}


// Refresh button (spins while syncing) + interval-picker dropdown. The one
// place a user goes to (a) manually re-sync or (b) change how often Watson
// syncs on its own. Sits in the header stats row so it's always in view.
function SyncControl({ syncing, trigger, lastSyncAt, intervalKey, onIntervalChange, onSyncClick }) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef(null)
  const current = SYNC_INTERVALS.find((i) => i.key === intervalKey) || SYNC_INTERVALS[0]

  // Click-outside dismisses the dropdown (no backdrop overlay — keeps the
  // control light, doesn't dim the rest of the page).
  useEffect(() => {
    if (!open) return
    const onDoc = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  const tooltip = syncing
    ? (trigger === 'auto' ? 'Auto-syncing (data was stale)…' : 'Syncing…')
    : lastSyncAt
      ? `Last synced ${relativeTimeFromNow(lastSyncAt)}`
      : 'Never synced'

  return (
    <div className="sync-control" ref={rootRef}>
      <button
        className={`sync-btn${syncing ? ' spinning' : ''}`}
        disabled={syncing}
        onClick={onSyncClick}
        title={tooltip}
        aria-label={tooltip}
      >
        {/* Circular arrow — same glyph most refresh buttons use. Spins via CSS
            when the `spinning` class is on. */}
        <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true">
          <path
            fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"
            d="M16.5 4v4h-4M3.5 16v-4h4M15 8a6 6 0 0 0-10.5 1M5 12a6 6 0 0 0 10.5-1"
          />
        </svg>
      </button>
      <button
        className="sync-interval-toggle"
        onClick={() => setOpen((v) => !v)}
        title="Auto-sync interval"
      >
        {current.label}
        <span className="sync-interval-chev">▾</span>
      </button>
      {open && (
        <div className="sync-interval-menu" role="listbox">
          {SYNC_INTERVALS.map((opt) => (
            <button
              key={opt.key}
              className={`sync-interval-item${opt.key === intervalKey ? ' active' : ''}`}
              onClick={() => { onIntervalChange(opt.key); setOpen(false) }}
              role="option"
              aria-selected={opt.key === intervalKey}
            >
              {opt.label}
              {opt.key === 'manual' && <span className="sync-interval-hint">no auto-sync</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}


function addDays(d, days) {
  const c = new Date(d)
  c.setDate(c.getDate() + days)
  return c
}


function toYMD(d) {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}


function formatDayLabel(d) {
  // Both sides must be normalized to local midnight — otherwise the time-of-
  // day carried on `d` (which is `now + offset*24h`, not midnight-shifted)
  // skews the integer diff as the wall clock advances through the day.
  // Bug repro before this fix: at 18:00, offset=-2 rounded to -1 → "Yesterday".
  const now = new Date()
  const todayMidnight = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const dMidnight = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const diff = Math.round((dMidnight - todayMidnight) / 86400000)
  if (diff === -1) return 'Yesterday'
  if (diff === 1) return 'Tomorrow'
  // e.g. "Mon, 14 Jul"
  return d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })
}


// ─── Timeline block + hover card ──────────────────────────────────────────
// The block is the pill on the timeline. Hovering it (or hovering the card
// that appears below) shows the meeting's details, with a Join button
// (Meet/Zoom link) so the user can jump straight into the call without a
// detour through Google Calendar's event page. Card sticks briefly after
// mouseleave so the user can move the cursor onto it without it vanishing.

function TimelineBlock({ meeting, left, width, isNow, lane = 0, maxLanes = 1 }) {
  const [show, setShow] = useState(false)
  const hideTimer = useRef(null)
  const showTimer = useRef(null)

  const cancelTimers = () => {
    if (hideTimer.current) { clearTimeout(hideTimer.current); hideTimer.current = null }
    if (showTimer.current) { clearTimeout(showTimer.current); showTimer.current = null }
  }
  const scheduleShow = () => {
    cancelTimers()
    showTimer.current = setTimeout(() => setShow(true), 180)
  }
  const scheduleHide = () => {
    cancelTimers()
    hideTimer.current = setTimeout(() => setShow(false), 140)
  }
  useEffect(() => cancelTimers, [])

  // Right-half blocks: card would overflow the right edge if left-aligned.
  // Flip it to right-align. Uses the block's LEFT percent as heuristic —
  // blocks past 55% get right-aligned relative to their wrapper.
  const rightAlign = left > 55

  // Vertical positioning by lane. Single-lane keeps the compact 56px
  // block within the 88px track; when events overlap, each lane gets a
  // proportional share of the (extra-tall) track. The track's own height
  // grew accordingly in the parent so blocks stay ~30-34px tall.
  const blockStyle = { left: `${left}%`, width: `${Math.max(width, 3)}%` }
  if (maxLanes > 1) {
    const TOP_PAD = 6
    const BOTTOM_PAD = 18   // room for hour labels along the bottom
    const GAP = 4
    const trackH = 54 + maxLanes * 34   // must match parent formula
    const usable = trackH - TOP_PAD - BOTTOM_PAD - GAP * (maxLanes - 1)
    const laneH = usable / maxLanes
    blockStyle.top = `${TOP_PAD + lane * (laneH + GAP)}px`
    blockStyle.height = `${laneH}px`
  }

  return (
    <div
      className={`day-timeline-block-wrap${rightAlign ? ' right-aligned' : ''}${maxLanes > 1 ? ' stacked' : ''}`}
      style={blockStyle}
      onMouseEnter={scheduleShow}
      onMouseLeave={scheduleHide}
    >
      <a
        className={`day-timeline-block${isNow ? ' now' : ''}`}
        href={meeting.html_link || '#'}
        target="_blank"
        rel="noreferrer"
        title={`${formatHM(new Date(meeting.start_at))} — ${meeting.title}`}
      >
        <div className="day-timeline-block-time">{formatHM(new Date(meeting.start_at))}</div>
        <div className="day-timeline-block-title">{meeting.title || '(no title)'}</div>
      </a>
      {show && (
        <MeetingHoverCard
          meeting={meeting}
          onMouseEnter={cancelTimers}
          onMouseLeave={scheduleHide}
        />
      )}
    </div>
  )
}


function MeetingHoverCard({ meeting, onMouseEnter, onMouseLeave }) {
  const start = new Date(meeting.start_at)
  const end = meeting.end_at ? new Date(meeting.end_at) : null
  const durationMin = end ? Math.round((end - start) / 60000) : null
  const attendees = meeting.attendees || []
  const description = cleanDescription(meeting.description)
  const provider = detectProvider(meeting.meet_link)

  return (
    <div className="hover-card" role="dialog" onMouseEnter={onMouseEnter} onMouseLeave={onMouseLeave}>
      <div className="hover-card-title">{meeting.title || '(no title)'}</div>
      <div className="hover-card-time">
        <span>{formatHM(start)}</span>
        {end && <span> – {formatHM(end)}</span>}
        {durationMin != null && <span className="hover-card-dim"> · {formatMinutes(durationMin)}</span>}
        {meeting.all_day && <span className="hover-card-dim"> · all-day</span>}
      </div>

      {(meeting.meet_link || meeting.html_link) && (
        <div className="hover-card-actions">
          {meeting.meet_link && (
            <a className="hover-card-btn primary" href={meeting.meet_link} target="_blank" rel="noreferrer">
              Join {provider}
            </a>
          )}
          {meeting.html_link && (
            <a className="hover-card-btn subtle" href={meeting.html_link} target="_blank" rel="noreferrer">
              Open in Calendar
            </a>
          )}
        </div>
      )}

      {meeting.location && !meeting.meet_link && (
        <div className="hover-card-row">
          <span className="hover-card-label">Location</span>
          <span className="hover-card-val">{meeting.location}</span>
        </div>
      )}

      {attendees.length > 0 && (
        <div className="hover-card-row">
          <span className="hover-card-label">Attendees</span>
          <span className="hover-card-val">
            {attendees.slice(0, 5).map(shortenEmail).join(', ')}
            {attendees.length > 5 && ` +${attendees.length - 5}`}
          </span>
        </div>
      )}

      {meeting.organizer && (
        <div className="hover-card-row">
          <span className="hover-card-label">Organizer</span>
          <span className="hover-card-val">{meeting.organizer}</span>
        </div>
      )}

      {description && (
        <div className="hover-card-desc">{description}</div>
      )}
    </div>
  )
}


function detectProvider(url) {
  if (!url) return ''
  if (/meet\.google\.com/i.test(url)) return 'Meet'
  if (/zoom\.us/i.test(url)) return 'Zoom'
  if (/teams\.microsoft\.com/i.test(url)) return 'Teams'
  if (/webex\.com/i.test(url)) return 'Webex'
  if (/meet\.jit\.si/i.test(url)) return 'Jitsi'
  return 'call'
}


function cleanDescription(desc) {
  if (!desc) return ''
  // Strip HTML tags Google sometimes packs in, collapse whitespace, cap length.
  const flat = String(desc).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()
  return flat.length > 260 ? flat.slice(0, 260) + '…' : flat
}


function shortenEmail(addr) {
  if (!addr || typeof addr !== 'string') return String(addr || '')
  const at = addr.indexOf('@')
  return at > 0 ? addr.slice(0, at) : addr
}


// ─── Attention inbox ──────────────────────────────────────────────────────
// Flock mentions + Watson-drafted pending actions merged and sorted by
// urgency. Order: unapproved pending → human mentions with hasMention →
// human unread DMs → bot alerts (collapsed to one row if there are many).

function AttentionInbox({ flock, pending, onChanged, onCapture }) {
  const items = useMemo(() => buildInbox(flock, pending), [flock, pending])
  if (items.length === 0) {
    return (
      <div className="inbox-empty">
        <div className="card-head">
          <div className="card-title">Pulling on you <span className="count">0</span></div>
        </div>
        <p className="today-panel-empty">Inbox is clear. Nothing pulling on you right now.</p>
      </div>
    )
  }
  return (
    <div className="inbox-wrap">
      <div className="card-head">
        <div className="card-title">Pulling on you <span className="count">{items.length}</span></div>
        <div className="card-action">Sorted by urgency</div>
      </div>
      <InboxScrollArea>
        {items.map((it) => (
          <InboxItem key={it.id} item={it} onChanged={onChanged} onCapture={onCapture} />
        ))}
      </InboxScrollArea>
    </div>
  )
}


function InboxScrollArea({ children }) {
  const { ref, hiddenBelow, showChip } = useScrollFade()
  return (
    <>
      <div ref={ref} className="inbox">{children}</div>
      {showChip && hiddenBelow > 0 && (
        <div className="scroll-more-chip">▾ {hiddenBelow} more</div>
      )}
    </>
  )
}


function MRTableBody({ children, empty }) {
  const { ref, hiddenBelow, showChip } = useScrollFade()
  return (
    <>
      <div ref={ref} className={`mr-table-body${empty ? ' no-overflow' : ''}`}>{children}</div>
      {showChip && hiddenBelow > 0 && (
        <div className="scroll-more-chip">▾ {hiddenBelow} more</div>
      )}
    </>
  )
}


function InboxItem({ item, onChanged, onCapture }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  const runAction = async (fn) => {
    setBusy(true); setErr(null)
    try { await fn(); onChanged?.() } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  if (item.kind === 'pending') {
    const a = item.raw
    const summary = pendingSummary(a)
    return (
      <div className="inbox-item pending">
        <div className="inbox-icon">✓</div>
        <div className="inbox-body">
          <div className="inbox-meta">
            <span className="who">Watson drafted</span>
            <span className="where">· {summary.kindLabel}</span>
          </div>
          <div className="inbox-text">{summary.body}</div>
          {err && <div className="inbox-error">{err}</div>}
        </div>
        <div className="inbox-actions">
          <button className="inbox-btn primary" disabled={busy} onClick={() => runAction(() => api.approveAction(a.id))}>Approve</button>
          <button className="inbox-btn subtle" disabled={busy} onClick={() => runAction(() => api.rejectAction(a.id))}>Reject</button>
        </div>
      </div>
    )
  }

  // Flock chat (human or bot). Item shape: { kind: 'flock', raw: chat, isBot, topMention }
  const chat = item.raw
  const isBot = item.isBot
  const top = item.topMention
  // Webhook entries are aggregated by channel — the primary label is the
  // channel name (senders vary per message), and we show ALL recent
  // messages inline rather than a single "top" pick.
  const isWebhook = chat.bucket === 'webhook'
  const captureSeed = top
    ? `Follow up: ${top.sender_name || top.sender} · ${chat.name} · "${(top.text || '').slice(0, 200)}"`
    : `Follow up: ${chat.name} · ${chat.unread_count} unread`
  return (
    <div className={`inbox-item ${isBot ? 'bots' : 'human'}`}>
      <div className="inbox-icon">{inboxIconFor(chat, top, isBot)}</div>
      <div className="inbox-body">
        <div className="inbox-meta">
          <span className="who">
            {isBot ? chat.name
              : isWebhook ? `#${chat.name}`
              : (top?.sender_name || top?.sender || chat.name)}
          </span>
          <span className="where">
            {isBot
              ? `· ${chat.mentions?.length || chat.unread_count} bot mention${(chat.mentions?.length || 1) === 1 ? '' : 's'}`
              : isWebhook
              ? `· ${chat.mentions?.length || 1} mention${(chat.mentions?.length || 1) === 1 ? '' : 's'} · ${relativeTimeFromNow(chat.last_message_time)}`
              : `· ${chat.is_group ? '#' : '@'}${chat.name} · ${relativeTimeFromNow(chat.last_message_time)}`}
          </span>
        </div>
        <div className={`inbox-text${isBot ? ' dim' : ''}`}>
          {isBot ? botDigest(chat)
            : isWebhook ? webhookDigest(chat)
            : (cleanText(top?.text) || chat.unread_count + ' unread messages')}
        </div>
        {err && <div className="inbox-error">{err}</div>}
      </div>
      <div className="inbox-actions">
        {isBot ? (
          <>
            <a className="inbox-btn" href={chat.url} target="_blank" rel="noreferrer">Open all</a>
            <button className="inbox-btn subtle" disabled title="Not wired yet">Dismiss</button>
          </>
        ) : (
          <>
            <a className="inbox-btn violet" href={chat.url} target="_blank" rel="noreferrer">Reply</a>
            <button className="inbox-btn" onClick={() => onCapture?.(captureSeed)}>Capture</button>
            {isWebhook && (
              <button
                className="inbox-btn subtle"
                disabled={busy}
                title="Clear these mentions from Watson (won't affect Flock; future @-mentions still land)"
                onClick={() => runAction(() => api.dismissFlockChannel(chat.jid))}
              >Dismiss</button>
            )}
          </>
        )}
      </div>
    </div>
  )
}


function buildInbox(flock, pending) {
  const items = []
  // Pending Watson-drafted actions first — they're user-facing decisions with
  // real side-effects.
  for (const a of pending || []) {
    items.push({ id: `p-${a.id}`, kind: 'pending', raw: a, priority: 0 })
  }
  // Flock items: human mentions > human unread > bot alerts.
  const bots = []
  for (const c of flock || []) {
    if (isBotChat(c)) { bots.push(c); continue }
    const top = topMention(c)
    const isRecent = recencyRank(c)
    // human with a @mention (top exists) ranks above human with just unread
    const priority = top ? 1 : 2
    items.push({
      id: `f-${c.jid}`, kind: 'flock', raw: c, isBot: false, topMention: top,
      priority: priority - Math.min(0.9, isRecent / 100),
    })
  }
  // Collapse all bots into ONE inbox item so they don't dominate.
  if (bots.length > 0) {
    // Pick the bot chat with the most recent activity as the representative.
    const rep = bots.slice().sort((a, b) => (b.last_message_time || '').localeCompare(a.last_message_time || ''))[0]
    // Sum mention counts across all bot chats so the badge is accurate.
    const totalMentions = bots.reduce((s, b) => s + (b.mentions?.length || 0), 0)
    items.push({
      id: 'bots',
      kind: 'flock',
      isBot: true,
      raw: { ...rep, _bots: bots, mentions: bots.flatMap((b) => b.mentions || []).slice(0, 10), unread_count: totalMentions },
      priority: 3,
    })
  }
  items.sort((a, b) => a.priority - b.priority)
  return items
}


function pendingSummary(a) {
  const p = a.payload || {}
  const kindLabel = ({
    clickup_close_task: 'close ClickUp task',
    clickup_create_task: 'new ClickUp task',
    clickup_comment: 'ClickUp comment',
    clickup_status: 'status change',
    link_mr_to_task: 'link MR ↔ task',
    merge_managed_tasks: 'merge duplicate cards',
    close_orphan_card: 'close orphan card',
    gcal_create_event: 'schedule meeting',
  }[a.kind]) || a.kind
  let body = ''
  if (a.kind === 'clickup_close_task') {
    // Lead with the Watson-owned task being closed (the user's review /
    // discussion task) so "which one?" is answered in one glance. Reason —
    // usually "Linked task is Closed" or "Linked MR was merged" — is the
    // trigger, appended as context.
    const name = a.target_task_name || `task ${a.target_id || ''}`.trim()
    const reason = p.reason || (p.merged_mr_title ? `MR "${p.merged_mr_title}" merged` : '')
    body = reason ? `${name} — ${reason}` : (p.draft || `Close ${name}`)
  } else if (a.kind === 'clickup_create_task') {
    const t = typeof p.draft === 'object' ? p.draft : {}
    body = t.name || String(p.draft || 'Create task')
  } else if (a.kind === 'link_mr_to_task') {
    body = `Link MR ${p.mr_id || ''} to ${p.task_name || a.target_id || 'task'}`
  } else if (a.kind === 'gcal_create_event') {
    // Render as "<title> · <time> · Meet" — reader sees the whole ask in one
    // glance. Attendees suppressed here (payload has them for the executor);
    // the Edit affordance is the place to review invitees.
    const d = (typeof p.draft === 'object' ? p.draft : {}) || {}
    const when = d.start_at ? formatEventTimeShort(d.start_at, d.end_at) : ''
    const bits = [d.title || 'Untitled meeting']
    if (when) bits.push(when)
    if (d.add_meet !== false) bits.push('Meet')
    if ((d.attendees || []).length) bits.push(`${d.attendees.length} attendee${d.attendees.length === 1 ? '' : 's'}`)
    body = bits.join(' · ')
  } else {
    body = typeof p.draft === 'string' ? p.draft : (p.reason || JSON.stringify(p).slice(0, 200))
  }
  return { kindLabel, body }
}


function formatEventTimeShort(startIso, endIso) {
  try {
    const s = new Date(startIso)
    if (isNaN(s)) return ''
    const day = s.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })
    const t = (d) => d.toTimeString().slice(0, 5)
    if (endIso) {
      const e = new Date(endIso)
      if (!isNaN(e)) return `${day} ${t(s)}–${t(e)}`
    }
    return `${day} ${t(s)}`
  } catch {
    return ''
  }
}


function isBotChat(c) {
  // Outgoing-Webhook-sourced entries are always real people @-mentioning
  // you in a channel — never bots. The sender-JID → name lookup often
  // misses (sidebar buddies and webhook senders use different JID formats),
  // so the fallback heuristic below would wrongly flag them as bots.
  if (c.bucket === 'webhook') return false
  // Heuristic for sidebar-DM entries: chat is a "bot" channel if all its
  // mention senders are unresolved (sender_name === sender_jid). That's how
  // the server signals "we couldn't map this sender to a buddy" — which
  // in practice means a bot / alert account.
  const ms = c.mentions || []
  if (ms.length === 0) return false
  return ms.every((m) => !m.sender_name || m.sender_name === m.sender)
}


function topMention(c) {
  const ms = c.mentions || []
  if (ms.length === 0) return null
  // Prefer the most recent; if messages have no timestamp we just take the first.
  return ms[0]
}


function recencyRank(c) {
  // Higher = more recent. We only have chat-level last_message_time here.
  if (!c.last_message_time) return 0
  const t = new Date(c.last_message_time).getTime()
  if (isNaN(t)) return 0
  const mins = (Date.now() - t) / 60000
  return Math.max(0, 100 - mins) // most-recent gets highest boost
}


function botDigest(chat) {
  const ms = chat.mentions || chat._bots?.flatMap((b) => b.mentions || []) || []
  const heads = ms.map((m) => firstLine(m.text)).filter(Boolean).slice(0, 3)
  return heads.length > 0 ? heads.join(' · ') + (ms.length > 3 ? ' · …' : '') : `${chat.unread_count || 0} alert${chat.unread_count === 1 ? '' : 's'}`
}


function webhookDigest(chat) {
  // Aggregated webhook entry: N @-mentions from possibly-different senders
  // in one channel. Render each on its own line "sender: message" so the
  // reader can scan who said what without opening Flock.
  const ms = chat.mentions || []
  if (ms.length === 0) return chat.unread_count + ' unread messages'
  return (
    <ul className="inbox-webhook-list">
      {ms.slice(0, 5).map((m, i) => (
        <li key={m.id || i}>
          <span className="sender">{m.sender_name || m.sender}:</span>{' '}
          <span className="text">{cleanText(m.text)}</span>
        </li>
      ))}
      {ms.length > 5 && <li className="more">…and {ms.length - 5} more</li>}
    </ul>
  )
}


function firstLine(t) {
  if (!t) return ''
  return String(t).split(/\n|\.|—/)[0].trim().slice(0, 90)
}


function cleanText(t) {
  if (!t) return ''
  return String(t).replace(/^@[^\s]+(?:\s[^\s]+)?\s+/, '').trim()
}


function inboxIconFor(chat, top, isBot) {
  if (isBot) return '⚠'
  const name = top?.sender_name || chat.name || '?'
  return initials(name)
}


function initials(name) {
  const parts = String(name).replace(/^[@#]/, '').split(/\s+/).filter(Boolean)
  if (parts.length === 0) return '?'
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
}


// ─── MR table ─────────────────────────────────────────────────────────────

function groupMRsByClickupId(mrs) {
  // Two MRs sharing the same clickup_id branch tag are the SAME logical
  // change split across repos (server + client, backend + frontend, etc).
  // Merge them into one group so the review queue reads as tasks, not
  // individual repos. MRs without a tag stay ungrouped (one per group).
  const groups = []
  const byId = new Map()
  for (const m of mrs) {
    const key = m.clickup_id
    if (key && byId.has(key)) {
      byId.get(key).mrs.push(m)
    } else {
      const g = { key: key || `single:${m.mr_id}`, mrs: [m] }
      groups.push(g)
      if (key) byId.set(key, g)
    }
  }
  return groups
}


function MRTable({ mrs, totalMRs, gitlabBase }) {
  // No slice cap — a scroll bar inside the card handles overflow. Keeps
  // the Today page height bounded regardless of review queue size, while
  // making every MR reachable without a trip to the GitLab dashboard.
  const groups = groupMRsByClickupId(mrs)
  const dashboardUrl = gitlabBase
    ? `${gitlabBase}/dashboard/merge_requests?state=opened&scope=all`
    : null
  return (
    <div className="mr-table">
      <div className="mr-table-head">
        <div className="mr-table-title">MRs to review <span className="count">{groups.length}</span></div>
        <div className="mr-row-stale">stale</div>
      </div>
      <MRTableBody empty={groups.length === 0}>
        {groups.length === 0 ? (
          <p className="today-panel-empty" style={{ padding: '10px 14px' }}>Review queue is clean.</p>
        ) : groups.map((g) => <MRRow key={g.key} group={g} />)}
      </MRTableBody>
      <div className="mr-table-foot">
        <span>{totalMRs} open · {groups.length} grouped</span>
        {dashboardUrl && (
          <a className="card-action" href={dashboardUrl} target="_blank" rel="noreferrer">GitLab →</a>
        )}
      </div>
    </div>
  )
}


// Lifecycle stage → { label, className } for the badge shown on each MR
// row. Reads from the `stage` field computed in gitlab_client._compute_stage.
const MR_STAGE_META = {
  merged:         { label: 'Uploaded',    cls: 'stage-merged' },
  qa:             { label: 'QA',          cls: 'stage-qa' },
  reviewed_by_me: { label: 'Reviewed',    cls: 'stage-reviewed' },
  review_pending: { label: 'To review',   cls: 'stage-pending' },
  closed:         { label: 'Closed',      cls: 'stage-closed' },
}


function MRStageBadge({ stage }) {
  const meta = MR_STAGE_META[stage]
  if (!meta) return null
  return <span className={`mr-stage-badge ${meta.cls}`}>{meta.label}</span>
}


function MRStageBadges({ stages }) {
  // Renders 0..N badges in workflow order (reviewed → qa → merged), so a
  // MR that's both reviewed_by_me and in QA shows BOTH tags side by side.
  if (!stages || stages.length === 0) return null
  return (
    <span className="mr-stage-badges">
      {stages.map((s) => <MRStageBadge key={s} stage={s} />)}
    </span>
  )
}


function MRRow({ group }) {
  const primary = group.mrs[0]
  // Max stale across the group — reviewing the whole cluster requires the
  // oldest MR's attention, so surface that.
  const maxStale = Math.max(...group.mrs.map((m) => staleDays(m.updated_at)))
  if (group.mrs.length === 1) {
    // Title-first layout mirrors the grouped case: title at top with the
    // stale-day chip aligned to its right, project + author + stages sit
    // in the sub-line beneath. Keeps the two card types visually consistent.
    return (
      <a className="mr-row" href={primary.url} target="_blank" rel="noreferrer">
        <div>
          <div className="mr-row-title">{primary.title}</div>
          <div className="mr-row-author">
            <span className="mr-row-project inline">{primary.project}</span>
            {' · '}{primary.author || '—'}
            {primary.role === 'engaged' ? ' · engaged' : ''}
            {(primary.stages?.length > 0) && <> · <MRStageBadges stages={primary.stages} /></>}
          </div>
        </div>
        <div className={`mr-row-stale${maxStale >= 3 ? ' hot' : ''}`}>{maxStale}d</div>
      </a>
    )
  }
  // Grouped: shared header (first MR's title, author), plus a project-chip
  // strip so each individual MR is still clickable. Chip carries its OWN
  // stage badge — the two projects can be at different stages (one merged,
  // one still under review) even though they're the same logical change.
  return (
    <div className="mr-row mr-row-grouped">
      <div>
        <div className="mr-row-title">{primary.title}</div>
        <div className="mr-row-author">
          {primary.author || '—'} · <span className="mr-group-badge">{group.mrs.length} MRs · same task</span>
        </div>
        <div className="mr-group-chips">
          {group.mrs.map((m) => (
            <a
              key={m.mr_id}
              className="mr-group-chip"
              href={m.url}
              target="_blank"
              rel="noreferrer"
              title={m.title}
            >
              {m.project}
              {(m.stages?.length > 0) && <MRStageBadges stages={m.stages} />}
            </a>
          ))}
        </div>
      </div>
      <div className={`mr-row-stale${maxStale >= 3 ? ' hot' : ''}`}>{maxStale}d</div>
    </div>
  )
}


function staleDays(updatedAt) {
  if (!updatedAt) return 0
  const t = new Date(updatedAt).getTime()
  if (isNaN(t)) return 0
  return Math.max(0, Math.floor((Date.now() - t) / 86400000))
}


// ─── Reminders (compact) ──────────────────────────────────────────────────

function ReminderList({ items, onChanged }) {
  const [busy, setBusy] = useState(null)
  const done = async (id) => {
    setBusy(id)
    try { await api.reminderDone(id); onChanged?.() }
    finally { setBusy(null) }
  }
  return (
    <div className="compact-card">
      <div className="compact-head">
        <div className="compact-title">Reminders <span className="count">{items.length}</span></div>
      </div>
      {items.length === 0 ? (
        <p className="today-panel-empty">Nothing due. Clear runway.</p>
      ) : (
        <RemindersScrollArea>
          {items.map((r) => (
            <div key={r.id} className="compact-item">
              <div className={`compact-dot${r.status === 'snoozed' ? ' snoozed' : ''}`}></div>
              <div className="compact-item-text">{r.text}</div>
              <div className="compact-item-due">
                {r.status === 'snoozed' ? 'snoozed' : formatDue(r.due_at)}
                <button className="inbox-btn subtle" disabled={busy === r.id} onClick={() => done(r.id)} style={{ marginLeft: 6 }}>Done</button>
              </div>
            </div>
          ))}
        </RemindersScrollArea>
      )}
    </div>
  )
}


function RemindersScrollArea({ children }) {
  const { ref, hiddenBelow, showChip } = useScrollFade()
  return (
    <>
      <div ref={ref} className="compact-list">{children}</div>
      {showChip && hiddenBelow > 0 && (
        <div className="scroll-more-chip">▾ {hiddenBelow} more</div>
      )}
    </>
  )
}


// ─── Capture bar ──────────────────────────────────────────────────────────

function CaptureBar({ value, onChange, onCaptured }) {
  // `busy` tracks WHICH action is running so we can disable the right
  // button and label it. Ask answers render above the input; a close
  // button clears them so the next capture doesn't have stale context.
  const [busy, setBusy] = useState(null)          // 'capture' | 'ask' | null
  const [error, setError] = useState(null)
  const [answer, setAnswer] = useState(null)
  const inputRef = useRef(null)

  // ⌘K's Capture / Ask actions dispatch these events; both just focus the
  // capture input (Today is already visible by the time they fire).
  useEffect(() => {
    const focusBox = () => inputRef.current?.focus()
    window.addEventListener('watson:focus-capture', focusBox)
    window.addEventListener('watson:focus-ask', focusBox)
    return () => {
      window.removeEventListener('watson:focus-capture', focusBox)
      window.removeEventListener('watson:focus-ask', focusBox)
    }
  }, [])

  const doCapture = async () => {
    const v = value.trim()
    if (!v || busy) return
    setBusy('capture'); setError(null); setAnswer(null)
    try {
      await api.capture(v)
      onChange('')
      onCaptured?.()
    } catch (e) { setError(e.message) } finally { setBusy(null) }
  }
  const doAsk = async () => {
    const v = value.trim()
    if (!v || busy) return
    setBusy('ask'); setError(null); setAnswer(null)
    try {
      const r = await api.ask(v)
      setAnswer(r)
    } catch (e) { setError(e.message) } finally { setBusy(null) }
  }
  const onKey = (e) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      if (e.shiftKey) doAsk()
      else doCapture()
    }
  }
  return (
    <div className="capture-bar-wrap">
      {answer && (
        <div className="capture-answer">
          <div className="capture-answer-head">
            <span className="capture-answer-label">Watson says</span>
            <button className="capture-answer-close" onClick={() => setAnswer(null)} title="Dismiss">×</button>
          </div>
          <Markdown text={answer.answer} />
        </div>
      )}
      <div className="capture-bar">
        <input
          ref={inputRef}
          className="capture-input"
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKey}
          placeholder="Capture a thought, ask Watson, or plan today…"
          disabled={!!busy}
        />
        <div className="capture-actions">
          <span className="capture-hint">
            <kbd>⌘</kbd><kbd>↵</kbd> capture · <kbd>⌘</kbd><kbd>⇧</kbd><kbd>↵</kbd> ask
          </span>
          {error && <span className="error-text">{error}</span>}
          <button className="capture-btn" disabled={!!busy || !value.trim()} onClick={doAsk}>
            {busy === 'ask' ? 'Asking…' : 'Ask'}
          </button>
          <button className="capture-btn primary" disabled={!!busy || !value.trim()} onClick={doCapture}>
            {busy === 'capture' ? 'Capturing…' : 'Capture'}
          </button>
        </div>
      </div>
    </div>
  )
}


// ─── time helpers ─────────────────────────────────────────────────────────

function isoToMs(iso) {
  if (!iso) return 0
  const t = new Date(iso).getTime()
  return isNaN(t) ? 0 : t
}

function timeIsoToMinutesInDay(iso) {
  if (!iso) return null
  const d = new Date(iso)
  if (isNaN(d.getTime())) return null
  return d.getHours() * 60 + d.getMinutes()
}

function minutesSinceTrackStart(d) {
  return d.getHours() * 60 + d.getMinutes() - TRACK_START_H * 60
}

function clampPct(v) {
  if (isNaN(v)) return null
  return Math.max(0, Math.min(100, v))
}

function formatHM(d) {
  const h = String(d.getHours()).padStart(2, '0')
  const m = String(d.getMinutes()).padStart(2, '0')
  return `${h}:${m}`
}

function formatDateLong(d) {
  return d.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })
}

function formatDue(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  const now = new Date()
  if (d.toDateString() === now.toDateString()) return formatHM(d)
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
}

function formatMinutes(mins) {
  if (!mins || mins <= 0) return '0m'
  const h = Math.floor(mins / 60)
  const m = Math.round(mins % 60)
  if (h === 0) return `${m}m`
  if (m === 0) return `${h}h`
  return `${h}h ${m}m`
}

function formatGap(minutes) {
  if (minutes < 30) return `${Math.round(minutes)}m`
  return formatMinutes(minutes) + ' focus'
}

function relativeTimeFromNow(iso) {
  if (!iso) return ''
  const t = new Date(iso).getTime()
  if (isNaN(t)) return ''
  const mins = Math.max(0, Math.round((Date.now() - t) / 60000))
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  return `${Math.floor(hrs / 24)}d ago`
}
