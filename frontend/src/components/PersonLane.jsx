import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { moveBoardCard } from '../boardOrder.js'
import { placementAround } from '../teamDrag.js'
import WorkCard from './WorkCard.jsx'
import { syncMonitor } from '../useSyncRefresh.js'

export function laneKey(lane) {
  return lane.person ? `person:${lane.person.id}` : `special:${lane.name}`
}

function itemOwnerKey(item) {
  return item.owner_person_id != null ? `person:${item.owner_person_id}` : `display:${item.owner_display || ''}`
}

export default function PersonLane({ lane, onItemsChange, onOpen, onAdd }) {
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [hasOverflow, setHasOverflow] = useState(false)
  const [atBottom, setAtBottom] = useState(true)
  const scrollRef = useRef(null)
  const dragOriginRef = useRef(null)
  const dragPayloadRef = useRef(null)
  const dragPlacementRef = useRef(null)
  const suppressOpenRef = useRef(false)
  const key = laneKey(lane)
  const items = lane.items || []
  const initials = lane.person ? lane.name.trim().split(/\s+/).filter(Boolean).slice(0, 2).map((part) => Array.from(part)[0]).join('').toLocaleUpperCase() : '–'

  useEffect(() => {
    const node = scrollRef.current
    if (!node) return undefined
    const inspect = () => {
      const overflows = node.scrollHeight > node.clientHeight + 1
      setHasOverflow(overflows)
      setAtBottom(!overflows || node.scrollTop + node.clientHeight >= node.scrollHeight - 1)
    }
    inspect()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(inspect)
    observer?.observe(node)
    return () => observer?.disconnect()
  }, [items.length])

  const boardForItems = () => ({ mode: 'flat', items })

  const persistMove = async (movedId, beforeId, afterId, rollbackItems = items) => {
    const item = items.find((entry) => entry.id === movedId)
    if (!item) return
    let updatedBoard
    try { updatedBoard = moveBoardCard(boardForItems(), movedId, item.state, beforeId, afterId) }
    catch (err) { setError(err.message); return }
    onItemsChange(updatedBoard.items)
    setError('')
    const release = syncMonitor.hold()
    try { await api.moveWork(movedId, { state: item.state, before_id: beforeId ?? null, after_id: afterId ?? null }) }
    catch { onItemsChange(rollbackItems); setError('Could not save the local priority order.') }
    finally { release() }
  }

  const beginPointer = (event, item) => {
    if (event.button !== 0) return
    const payload = { itemId: item.id, lane: key, owner: itemOwnerKey(item), state: item.state }
    dragOriginRef.current = items
    dragPayloadRef.current = payload
    dragPlacementRef.current = null
  }
  const activePayload = () => dragPayloadRef.current
  const rejectCrossDrop = (payload, target) => {
    if (!payload) { setNotice('That card cannot be reordered here.'); return true }
    if (payload.lane !== key) { setNotice('Cards stay in their owner lane. Assignment is read-only here.'); return true }
    if (target && payload.owner !== itemOwnerKey(target)) { setNotice('Cards can only be reordered with work from the same owner.'); return true }
    return false
  }
  const previewAround = (event, target) => {
    const payload = activePayload()
    if (!payload) return
    if (rejectCrossDrop(payload, target) || payload.itemId === target.id) return
    const placement = placementAround(items, payload.itemId, target.id, payload.state)
    let preview
    try { preview = moveBoardCard(boardForItems(), payload.itemId, payload.state, placement.beforeId, placement.afterId) }
    catch (err) { setError(err.message); return }
    dragPlacementRef.current = placement
    onItemsChange(preview.items)
  }
  const previewAtEnd = () => {
    const payload = activePayload()
    if (!payload) return
    if (rejectCrossDrop(payload, items.at(-1))) return
    const lastId = items.at(-1)?.id
    if (payload.itemId === lastId) return
    const placement = { state: payload.state, afterId: lastId }
    let preview
    try { preview = moveBoardCard(boardForItems(), payload.itemId, placement.state, undefined, lastId) }
    catch (err) { setError(err.message); return }
    dragPlacementRef.current = placement
    onItemsChange(preview.items)
  }
  const previewFromMouse = (event) => {
    if (!activePayload()) return
    const card = event.target.closest?.('.person-lane-card')
    if (card) {
      const target = items.find((item) => String(item.id) === card.dataset.workItemId)
      if (target) previewAround(event, target)
      return
    }
    const end = event.target.closest?.('.person-lane-drop-end')
    if (end) previewAtEnd()
  }
  const resetPointer = () => {
    dragOriginRef.current = null
    dragPayloadRef.current = null
    dragPlacementRef.current = null
  }
  const finishPointer = () => {
    const payload = activePayload()
    const placement = dragPlacementRef.current
    const rollbackItems = dragOriginRef.current || items
    if (payload && placement) {
      suppressOpenRef.current = true
      persistMove(payload.itemId, placement.beforeId, placement.afterId, rollbackItems)
    }
    resetPointer()
  }
  const cancelPointer = () => {
    if (dragOriginRef.current) onItemsChange(dragOriginRef.current)
    resetPointer()
  }
  const openCard = (item) => {
    if (suppressOpenRef.current) { suppressOpenRef.current = false; return }
    onOpen?.(item)
  }

  const renderCard = (item) => <div className="person-lane-card" data-work-item-id={item.id} key={item.id}>
    <WorkCard item={item} onOpen={openCard} wholeCardDrag onMouseDown={beginPointer} />
  </div>

  return <section className={`person-lane${lane.person?.is_self ? ' is-self' : ''}`} aria-labelledby={`lane-${key}`} onMouseMove={previewFromMouse} onMouseUp={finishPointer} onMouseLeave={cancelPointer}>
    <header className="person-lane-heading"><div className="person-lane-identity"><span className="person-avatar" aria-hidden="true">{initials}</span><div><h2 id={`lane-${key}`}>{lane.name}</h2><p>{lane.person ? (lane.person.is_self ? 'You' : 'Team member') : lane.name === 'Others' ? 'Other collaborators' : 'Without an owner'}</p></div></div><div className="person-lane-meta">{onAdd && <button type="button" className="person-lane-add" onClick={onAdd} aria-label={`Add work for ${lane.name}`} title="Add work">+</button>}<span>{items.length}</span></div></header>
    {notice && <p className="sr-only" aria-live="polite">{notice}</p>}
    {error && <p className="work-inline-error" role="alert">{error}</p>}
    <div className="person-lane-scroll" ref={scrollRef} onScroll={() => {
      const node = scrollRef.current
      if (node) setAtBottom(node.scrollTop + node.clientHeight >= node.scrollHeight - 1)
    }}>
      <div className="person-lane-flat" aria-label={`${lane.name} priority`}>
        {items.map((item) => renderCard(item))}
        <div className="person-lane-drop-end" aria-label={`Move work to end of ${lane.name} priority`} />
      </div>
      {!items.length && <p className="person-lane-empty">No work here yet.</p>}
    </div>
    {hasOverflow && !atBottom && <div className="person-lane-fade" aria-hidden="true" />}
  </section>
}
