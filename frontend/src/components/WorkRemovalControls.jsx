export default function WorkRemovalControls({ removedAt, busy = false, onRequestRemove, onRestore }) {
  if (removedAt) return <div className="work-removed-banner" role="status">
    <div><strong>Removed from Watson</strong><p>This detail is still available. Restoring keeps its previous state; completed or ignored work may remain off active boards.</p></div>
    <button type="button" disabled={busy} onClick={onRestore}>{busy ? 'Restoring…' : 'Undo / Restore'}</button>
  </div>
  return <details className="work-removal-menu">
    <summary aria-label="Work actions">⋯</summary>
    <div><button type="button" disabled={busy} onClick={event => {
      const menu = event.currentTarget.closest('details')
      menu.open = false
      menu.querySelector('summary').focus()
      onRequestRemove?.()
    }}>Remove from Watson</button></div>
  </details>
}
