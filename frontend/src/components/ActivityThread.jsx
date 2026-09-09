const ACTIVITY_LABELS = {
  note: 'Note',
  decision: 'Decision',
  blocker: 'Blocker',
  system: 'System',
  capture: 'Capture',
  external_action: 'External action',
}

function timestamp(value) {
  if (!value) return ''
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

export default function ActivityThread({ activity = [] }) {
  return <section className="activity-thread" aria-labelledby="activity-title">
    <div className="work-section-heading"><h2 id="activity-title">Activity</h2><span>{activity.length}</span></div>
    {!activity.length ? <p className="work-detail-empty">No activity yet. Add a note, decision, or blocker to keep the context here.</p> : <ol className="activity-list">
      {activity.map((entry) => <li key={entry.id} className={`activity-item activity-${entry.activity_type || 'system'}`}>
        <div><span className="activity-type">{ACTIVITY_LABELS[entry.activity_type] || 'Activity'}</span><time dateTime={entry.created_at}>{timestamp(entry.created_at)}</time></div>
        <p>{entry.body || 'No details supplied.'}</p>
      </li>)}
    </ol>}
  </section>
}
