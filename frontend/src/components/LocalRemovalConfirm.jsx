import { useEffect, useId, useRef } from 'react'

export default function LocalRemovalConfirm({ title, actionLabel, busy = false, error = '', onConfirm, onCancel }) {
  const dialog = useRef(null)
  const headingId = useId()
  const descriptionId = useId()
  useEffect(() => {
    const node = dialog.current
    node.showModal()
    return () => { if (node.open) node.close() }
  }, [])
  return <dialog ref={dialog} className="local-removal-dialog" aria-labelledby={headingId} aria-describedby={descriptionId}
    onCancel={event => { event.preventDefault(); if (!busy) onCancel?.() }}>
    <h2 id={headingId}>{title}</h2>
    <p id={descriptionId}>Only removes from Watson. ClickUp tasks and GitLab MRs are unchanged.</p>
    <p>You can restore this later.</p>
    {error && <p className="work-inline-error" role="alert">{error}</p>}
    <div className="local-removal-actions">
      <button type="button" autoFocus disabled={busy} onClick={onCancel}>Cancel</button>
      <button type="button" className="local-removal-danger" disabled={busy} onClick={onConfirm}>{busy ? 'Removing…' : actionLabel}</button>
    </div>
  </dialog>
}
