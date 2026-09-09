import { groupLinkedWork } from '../linkedWorkGroups.js'

function safeExternalUrl(value) {
  try {
    const url = new URL(value)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : ''
  } catch { return '' }
}

function sourceLabel(link, url) {
  if (link.source_type === 'clickup') return url || 'ClickUp task'
  if (link.label) return link.label
  if (link.source_type === 'gitlab_mr') return `GitLab MR ${link.external_id || ''}`.trim()
  return link.external_id || link.source_type || 'External link'
}

export default function LinkedWork({ links = [] }) {
  const groups = groupLinkedWork(links)
  return <section className="linked-work" aria-labelledby="linked-work-title">
    <div className="work-section-heading"><h2 id="linked-work-title">Linked work</h2><span>{links.length}</span></div>
    {!links.length ? <p className="work-detail-empty">No external work is linked yet.</p> : <ul className="linked-work-groups">
      {groups.map((group) => <li className="linked-work-group" key={group.key}>
        <div className="linked-work-group-heading"><h3>{group.label}</h3><span>{group.links.length}</span></div>
        <ul className="linked-work-list">{group.links.map((link) => {
          const url = safeExternalUrl(link.url)
          const label = sourceLabel(link, url)
          return <li key={link.id || `${link.source_type}-${link.external_id}`}>
            {url ? <a href={url} target="_blank" rel="noopener noreferrer">{label}<span className="sr-only"> (opens in a new tab)</span></a> : <span className="linked-work-missing">{label}</span>}
          </li>
        })}</ul>
      </li>)}
    </ul>}
  </section>
}
