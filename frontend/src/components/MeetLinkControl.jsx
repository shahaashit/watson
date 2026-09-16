import { useId, useSyncExternalStore } from 'react'
import { api } from '../api.js'
import { createMeetLinkController } from '../meetLinkController.js'

const controller = createMeetLinkController({
  create: requestId => api.createMeetLink(requestId),
  copy: url => navigator.clipboard.writeText(url),
  uuid: () => crypto.randomUUID(),
  storage: {
    getItem: key => typeof window === 'undefined' ? null : window.sessionStorage.getItem(key),
    setItem: (key, value) => window.sessionStorage.setItem(key, value),
  },
})

export default function MeetLinkControl() {
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  const noteId = useId()
  return <div className="meet-link-control">
    <div className="meet-link-actions">
      <button type="button" className="meet-link-create" aria-label="New Meet · Copy link" aria-describedby={noteId}
        disabled={state.busy}
        title="Copy a short Meet nickname link for your organization. Choose your work Google account when opening it; the meeting is not created in advance."
        onClick={() => controller.start()}>{state.busy ? 'Generating…' : state.copied ? '✓ Link copied' : 'New Meet'}</button>
    </div>
    <span id={noteId} className="meet-link-sr-only">For your Google Workspace organization only. Choose your work account when opening the link. No invitations or calendar events. The meeting is not created in advance.</span>
    {state.message && <p className={state.copied ? 'meet-link-sr-only' : state.code ? 'meet-link-error' : 'meet-link-note'} role={state.code ? 'alert' : 'status'}>{state.message}</p>}
    {state.url && <div className="meet-link-result">
      <input aria-label="Meet link" value={state.url} readOnly onFocus={event => event.currentTarget.select()} />
      <button type="button" disabled={state.busy} onClick={() => controller.copyAgain()}>Copy link</button>
      <button type="button" disabled={state.busy} onClick={() => controller.dismiss()} aria-label="Dismiss Meet link">✕</button>
    </div>}
  </div>
}
