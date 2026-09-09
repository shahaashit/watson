export default function WorkCard({ item, index, total, onMoveUp, onMoveDown, onOpen, onDragStart, onDragEnd, onMouseDown, wholeCardDrag = false }) {
  const timestamp = item.updated_at ? new Date(item.updated_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : ''
  const repositories = item.repositories || []

  return (
    <article className="work-card" onDoubleClick={() => onOpen?.(item)}>
      <button className="work-card-main" data-drag-surface={wholeCardDrag || undefined} onMouseDown={wholeCardDrag ? (event) => onMouseDown?.(event, item) : undefined} onClick={() => onOpen?.(item)} aria-label={`Open ${item.title}`}>
        <span className="work-card-title" title={item.title}>{item.title}</span>
        {!!repositories.length && <span className="work-card-repositories" aria-label="GitLab repositories">
          {repositories.map((repository) => <span className="work-card-repository" title={repository} key={repository}>
            {repository.split('/').filter(Boolean).at(-1)}
          </span>)}
        </span>}
        {item.description && <span className="work-card-description">{item.description}</span>}
        {timestamp && <span className="work-card-date">Updated {timestamp}</span>}
      </button>
      {!wholeCardDrag && <div className="work-card-actions" aria-label={`Actions for ${item.title}`}>
        <button type="button" className="work-drag-handle" draggable onDragStart={(event) => onDragStart?.(event, item)} onDragEnd={onDragEnd} aria-label={`Reorder ${item.title}`} title="Drag to reorder">⋮⋮</button>
        <button type="button" onClick={() => onMoveUp?.(item)} disabled={index === 0} aria-label={`Move ${item.title} up`} title="Move Up">↑</button>
        <button type="button" onClick={() => onMoveDown?.(item)} disabled={index === total - 1} aria-label={`Move ${item.title} down`} title="Move Down">↓</button>
      </div>}
    </article>
  )
}
