const STATES = ['today', 'next', 'waiting', 'done']

function insert(items, moved, beforeId, afterId) {
  const remaining = items.filter((item) => item.id !== moved.id)
  let index = remaining.length
  if (beforeId != null) {
    index = remaining.findIndex((item) => item.id === beforeId)
  } else if (afterId != null) {
    index = remaining.findIndex((item) => item.id === afterId) + 1
  }
  if (index < 0) throw new Error('Placement target is not on this board.')
  return [...remaining.slice(0, index), moved, ...remaining.slice(index)]
}

export function moveBoardCard(board, movedId, destinationState, beforeId, afterId) {
  if (beforeId != null && afterId != null) throw new Error('Choose one placement target.')
  const sourceItems = board.mode === 'flat'
    ? (board.items || [])
    : STATES.flatMap((state) => board[state] || [])
  const source = sourceItems.find((item) => item.id === movedId)
  if (!source) throw new Error('The moved card is no longer on this board.')

  if (board.mode === 'flat') {
    return { ...board, items: insert(board.items || [], source, beforeId, afterId) }
  }

  const moved = { ...source, state: destinationState }
  const next = { ...board }
  for (const state of STATES) {
    const withoutMoved = (board[state] || []).filter((item) => item.id !== movedId)
    next[state] = state === destinationState
      ? insert(withoutMoved, moved, beforeId, afterId)
      : withoutMoved
  }
  next.items = (board.items || []).map((item) => item.id === movedId ? moved : item)
  return next
}

export function changeBoardCardState(board, movedId, destinationState) {
  if (board.mode !== 'flat') {
    return moveBoardCard(board, movedId, destinationState)
  }
  if (!(board.items || []).some((item) => item.id === movedId)) {
    throw new Error('The moved card is no longer on this board.')
  }
  return {
    ...board,
    items: destinationState === 'done'
      ? board.items.filter((item) => item.id !== movedId)
      : board.items.map((item) => item.id === movedId
        ? { ...item, state: destinationState }
        : item),
  }
}
