const GROUP_LABELS = {
  clickup: 'ClickUp',
  gitlab_mr: 'GitLab MRs',
  url: 'Links',
}

function groupLabel(sourceType) {
  if (GROUP_LABELS[sourceType]) return GROUP_LABELS[sourceType]
  return sourceType.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
}

export function groupLinkedWork(links = []) {
  const groups = new Map()
  for (const link of links) {
    const key = link.source_type || 'link'
    if (!groups.has(key)) groups.set(key, { key, label: groupLabel(key), links: [] })
    groups.get(key).links.push(link)
  }
  return [...groups.values()]
}
