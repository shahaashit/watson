import { useId, useRef, useState } from 'react'
import { groupLinkedWork } from '../linkedWorkGroups.js'
import { submitLinkedMr } from '../linkedMrForm.js'

function safeExternalUrl(value) {
  try {
    const url = new URL(value)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : ''
  } catch { return '' }
}

function sourceLabel(link, url) {
  if (link.source_type === 'clickup') return { original: 'Original task', review: 'Review task' }[link.task_kind] || 'ClickUp task'
  if (link.source_type === 'gitlab_mr' && url) {
    try {
      const match = new URL(url).pathname.match(/^(.*?)\/(?:-\/)?merge_requests\/\d+\/?$/)
      const repo = match?.[1].split('/').filter(Boolean).pop()
      if (repo) return decodeURIComponent(repo)
    } catch { /* Preserve the existing descriptive fallback for malformed links. */ }
  }
  if (link.label) return link.label
  if (link.source_type === 'gitlab_mr') return `GitLab MR ${link.external_id || ''}`.trim()
  return link.external_id || link.source_type || 'External link'
}

export default function LinkedWork({ links = [], removedLinks = [], onAddMr, onRestore, busy = false, removed = false }) {
  const [adding, setAdding] = useState(false)
  const [urlInput, setUrlInput] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState('')
  const submitPending = useRef(false)
  const formId = useId()
  const blocked = busy || submitting
  const canAdd = Boolean(onAddMr) && !removed
  const groups = groupLinkedWork(links)
  const count = groups.reduce((total, group) => total + group.links.length, 0)
  if (canAdd && !groups.some(group => group.key === 'gitlab_mr')) groups.push({ key: 'gitlab_mr', label: 'GitLab MRs', links: [] })
  const submit = async event => {
    event.preventDefault()
    if (!canAdd || blocked || submitPending.current) return
    submitPending.current = true
    setSubmitting(true); setFormError('')
    try {
      await submitLinkedMr(urlInput, onAddMr, () => { setUrlInput(''); setAdding(false) })
    } catch { setFormError('Could not add this MR. Check the URL and try again.') }
    finally { submitPending.current = false; setSubmitting(false) }
  }
  return <section className="linked-work" aria-labelledby="linked-work-title">
    <div className="work-section-heading"><h2 id="linked-work-title">Linked work</h2><span>{count}</span></div>
    {!count && <p className="work-detail-empty">No external work is linked yet.</p>}
    {!!groups.length && <ul className="linked-work-groups">
      {groups.map((group) => <li className="linked-work-group" key={group.key}>
        <div className="linked-work-group-heading"><h3>{group.label}</h3><span>{group.links.length}</span>
          {group.key === 'gitlab_mr' && canAdd && <button type="button" className="linked-work-add-toggle" disabled={blocked} aria-expanded={adding} aria-controls={adding ? formId : undefined} onClick={() => setAdding(value => !value)}>+ Add MR</button>}
        </div>
        {group.key === 'gitlab_mr' && canAdd && adding && <form id={formId} className="linked-work-add-form" onSubmit={submit}>
          <label htmlFor={`${formId}-url`}>GitLab MR URL</label>
          <input id={`${formId}-url`} type="url" autoFocus required value={urlInput} disabled={blocked} onChange={event => setUrlInput(event.target.value)} placeholder="https://gitlab.example.com/project/-/merge_requests/12" />
          <div><button type="submit" disabled={blocked || !urlInput.trim()}>{submitting ? 'Adding…' : 'Add'}</button><button type="button" disabled={blocked} onClick={() => { setAdding(false); setFormError('') }}>Cancel</button></div>
          {formError && <p className="work-inline-error" role="alert">{formError}</p>}
        </form>}
        <ul className="linked-work-list">{group.links.map((link) => {
          const taskId = String(link.external_id ?? '').trim()
          const fallbackUrl = link.source_type === 'clickup' && /^[a-zA-Z0-9]+$/.test(taskId)
            ? `https://app.clickup.com/t/${taskId}` : ''
          const url = safeExternalUrl(link.url) || fallbackUrl
          const label = sourceLabel(link, url)
          return <li key={link.id || `${link.source_type}-${link.external_id}`}>
            {url ? <a href={url} target="_blank" rel="noopener noreferrer" title={link.source_type === 'gitlab_mr' ? `${link.label || 'Merge request'} (${link.external_id || ''})` : undefined}>{label}<span className="sr-only"> (opens in a new tab)</span></a> : <span className="linked-work-missing">{label}</span>}
          </li>
        })}</ul>
      </li>)}
    </ul>}
    {removedLinks.some(link => link.source_type === 'gitlab_mr') && <div className="removed-mr-links"><h3>Removed MR links</h3><ul className="linked-work-list">
      {removedLinks.filter(link => link.source_type === 'gitlab_mr').map(link => <li key={link.id}>
        <span>{link.label || 'GitLab MR'} ({link.external_id || link.id})</span>
        {onRestore && <button type="button" className="linked-work-local-action" disabled={busy} onClick={() => onRestore(link)} aria-label={`Restore ${link.label || 'GitLab MR'} (${link.external_id || link.id})`}>Restore link</button>}
      </li>)}
    </ul></div>}
  </section>
}
