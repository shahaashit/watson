export function placementAround(items, movedId, targetId, state) {
  const movedIndex = items.findIndex((item) => item.id === movedId)
  const targetIndex = items.findIndex((item) => item.id === targetId)
  return movedIndex >= 0 && movedIndex < targetIndex
    ? { state, afterId: targetId }
    : { state, beforeId: targetId }
}
