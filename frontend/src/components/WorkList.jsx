import { useState } from 'react'
import WorkCard from './WorkCard.jsx'

const TRANSFER_TYPE = 'application/x-watson-work'

function movedId(event) {
  try { return JSON.parse(event.dataTransfer.getData(TRANSFER_TYPE)).itemId }
  catch { return Number(event.dataTransfer.getData('text/plain')) || null }
}

export default function WorkList({ title, state, items = [], onMove, onOpen, error = '' }) {
  const [dropTarget, setDropTarget] = useState(null)

  const dragStart = (event, item) => {
    event.dataTransfer.effectAllowed = 'move'
    event.dataTransfer.setData(TRANSFER_TYPE, JSON.stringify({ itemId: item.id }))
    event.dataTransfer.setData('text/plain', String(item.id))
  }
  const dropBefore = (event, beforeId) => {
    event.preventDefault()
    const itemId = movedId(event)
    setDropTarget(null)
    if (itemId && itemId !== beforeId) onMove?.(itemId, state, beforeId)
  }
  const dropAtEnd = (event) => {
    event.preventDefault()
    const itemId = movedId(event)
    const lastId = items.at(-1)?.id
    setDropTarget(null)
    if (itemId && itemId !== lastId) onMove?.(itemId, state, undefined, lastId)
  }

  return (
    <section className="work-list" aria-label={title}>
      <div className="work-list-heading"><h2>{title}</h2><span>{items.length}</span></div>
      {error && <p className="work-inline-error" role="alert">{error}</p>}
      <div className="work-list-items">
        {items.map((item, index) => (
          <div key={item.id} className={`work-card-wrap${dropTarget === item.id ? ' is-drop-target-before' : ''}`} onDragOver={(event) => { event.preventDefault(); setDropTarget(item.id) }} onDragLeave={() => setDropTarget(null)} onDrop={(event) => dropBefore(event, item.id)}>
            <WorkCard item={item} index={index} total={items.length}
              onMoveUp={() => onMove?.(item.id, state, items[index - 1]?.id)}
              onMoveDown={() => onMove?.(item.id, state, undefined, items[index + 1]?.id)}
              onOpen={onOpen}
              onDragStart={dragStart} onDragEnd={() => setDropTarget(null)} />
          </div>
        ))}
        <div className={`work-drop-end${dropTarget === 'end' ? ' is-drop-target-end' : ''}`} onDragOver={(event) => { event.preventDefault(); setDropTarget('end') }} onDragLeave={() => setDropTarget(null)} onDrop={dropAtEnd} aria-label={`Move work to end of ${title}`} />
        {!items.length && <p className="work-list-empty">Nothing here yet.</p>}
      </div>
    </section>
  )
}
