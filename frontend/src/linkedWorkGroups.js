const GROUP_LABELS = {
  clickup: 'ClickUp',
  gitlab_mr: 'GitLab MRs',
  url: 'Links',
}

function groupLabel(sourceType) {
  if (GROUP_LABELS[sourceType]) return GROUP_LABELS[sourceType]
  return sourceType.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
}

function canonicalClickupId(link) {
  if (link.source_type !== 'clickup') return null
  if (!link.url) {
    const id = String(link.external_id ?? '').trim()
    return /^[a-zA-Z0-9]+$/.test(id) ? id : null
  }
  try {
    const url = new URL(link.url)
    if (url.protocol !== 'https:' || url.hostname !== 'app.clickup.com' || url.port || url.username || url.password) return null
    return url.pathname.match(/^\/t\/([a-zA-Z0-9]+)\/?$/)?.[1] || null
  } catch { return null }
}

export function groupLinkedWork(links = []) {
  const groups = new Map()
  const clickupIds = new Map()
  for (const link of links) {
    const key = link.source_type || 'link'
    if (!groups.has(key)) groups.set(key, { key, label: groupLabel(key), links: [] })
    const rows = groups.get(key).links
    const taskId = canonicalClickupId(link)
    if (taskId && clickupIds.has(taskId)) {
      const index = clickupIds.get(taskId)
      if (!['original', 'review'].includes(rows[index].task_kind) && ['original', 'review'].includes(link.task_kind)) {
        rows[index] = { ...rows[index], task_kind: link.task_kind }
      }
      continue
    }
    if (taskId) clickupIds.set(taskId, rows.length)
    rows.push(link)
  }
  return [...groups.values()]
}
