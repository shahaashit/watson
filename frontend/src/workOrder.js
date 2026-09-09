export function reorderIds(ids, movedId, beforeId, afterId) {
  if (new Set(ids).size !== ids.length) {
    throw new Error('Input ids must be unique')
  }
  if (!ids.includes(movedId)) throw new Error('Moved id is not in this list')
  if (beforeId != null && afterId != null) {
    throw new Error('Provide either a before or after target, not both before and after')
  }
  if (beforeId == null && afterId == null) {
    throw new Error('Provide exactly one placement target')
  }
  const targetId = beforeId ?? afterId
  if (targetId === movedId) {
    throw new Error('Cannot move an id relative to itself')
  }

  const ordered = ids.filter((id) => id !== movedId)
  const targetIndex = ordered.indexOf(targetId)
  if (targetIndex === -1) {
    throw new Error(`${beforeId != null ? 'Before' : 'After'} id is not in this list`)
  }

  const insertionIndex = beforeId != null ? targetIndex : targetIndex + 1
  ordered.splice(insertionIndex, 0, movedId)
  return ordered
}
