import { useState } from 'react'

const TRANSFER_TYPE = 'application/x-watson-work'

function clickupLink(value) {
  try {
    const url = new URL(value)
    return url.protocol === 'https:' && url.hostname === 'app.clickup.com' && !url.username && !url.password ? url.href : ''
  } catch { return '' }
}

export default function PersonalTaskList({ items = [], onMove, onOpen }) {
  const [dropTarget, setDropTarget] = useState(null)
  const dragStart = (event, item) => {
    event.dataTransfer.effectAllowed = 'move'
    event.dataTransfer.setData(TRANSFER_TYPE, JSON.stringify({ itemId: item.id }))
    event.dataTransfer.setData('text/plain', String(item.id))
  }
  const drop = (event, beforeId, afterId) => {
    event.preventDefault()
    setDropTarget(null)
    let itemId
    try { itemId = JSON.parse(event.dataTransfer.getData(TRANSFER_TYPE)).itemId }
    catch { return }
    const item = items.find(entry => entry.id === itemId)
    if (item && itemId !== beforeId && itemId !== afterId) onMove?.(itemId, item.state, beforeId, afterId)
  }

  return <div className="personal-task-list" role="list" aria-label="My tasks">
    {items.map((item, index) => {
      const url = clickupLink(item.clickup_url)
      return <div key={item.id} role="listitem"
        className={`personal-task-row work-card-wrap${dropTarget === item.id ? ' is-drop-target-before' : ''}`}
        onDragOver={event => { event.preventDefault(); setDropTarget(item.id) }}
        onDragLeave={() => setDropTarget(null)} onDrop={event => drop(event, item.id)}>
        <div className="personal-task-content">
          <button type="button" className="personal-task-open" onClick={() => onOpen?.(item)} aria-label={`Open ${item.title}`}>
            <span className="personal-task-title" title={item.title}>{item.title}</span>
          </button>
          {url && <a className="personal-task-source" href={url} target="_blank" rel="noreferrer" aria-label={`Open ${item.title} in ClickUp`}>ClickUp <span aria-hidden="true">↗</span></a>}
        </div>
        <div className="personal-task-actions" aria-label={`Reorder ${item.title}`}>
          <button type="button" className="personal-task-drag" draggable onDragStart={event => dragStart(event, item)} onDragEnd={() => setDropTarget(null)} aria-label={`Reorder ${item.title}`} title="Drag to reorder">⋮⋮</button>
          <button type="button" onClick={() => onMove?.(item.id, item.state, items[index - 1]?.id)} disabled={index === 0} aria-label={`Move ${item.title} up`} title="Move up">↑</button>
          <button type="button" onClick={() => onMove?.(item.id, item.state, undefined, items[index + 1]?.id)} disabled={index === items.length - 1} aria-label={`Move ${item.title} down`} title="Move down">↓</button>
        </div>
      </div>
    })}
    {!!items.length && <div className={`personal-task-drop-end${dropTarget === 'end' ? ' is-drop-target-end' : ''}`}
      onDragOver={event => { event.preventDefault(); setDropTarget('end') }}
      onDragLeave={() => setDropTarget(null)} onDrop={event => drop(event, undefined, items.at(-1)?.id)} aria-label="Move work to end of My tasks" />}
    {!items.length && <p className="personal-tasks-empty">No active tasks. Add a task or capture what’s on your mind.</p>}
  </div>
}
