export default function RemovedWorkList({ items = [], loading = false, error = '', busyId = null, onRestore, onRetry }) {
  return <>
    {loading && <p className="settings-muted" role="status">Loading removed work…</p>}
    {error && <div className="settings-error" role="alert"><p>{error}</p><button type="button" disabled={loading || busyId !== null} onClick={onRetry}>Retry</button></div>}
    {!!items.length && <ul className="removed-work-list">{items.map(item => <li key={item.id}>
      <a href={`/work/${encodeURIComponent(item.id)}`}>{item.title}</a>
      <button type="button" disabled={busyId !== null} onClick={() => onRestore?.(item.id)} aria-label={`Restore ${item.title}`}>{busyId === item.id ? 'Restoring…' : 'Restore'}</button>
    </li>)}</ul>}
    {!loading && !error && !items.length && <p className="settings-muted">No removed work.</p>}
  </>
}
