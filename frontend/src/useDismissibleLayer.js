import { useEffect, useRef } from 'react'

export function shouldDismissLayer(root, event) {
  if (event.type === 'keydown') return event.key === 'Escape'
  return Boolean(root && event.type === 'pointerdown' && !root.contains(event.target))
}

export default function useDismissibleLayer({ open, onDismiss }) {
  const rootRef = useRef(null)
  const onDismissRef = useRef(onDismiss)

  useEffect(() => { onDismissRef.current = onDismiss }, [onDismiss])
  useEffect(() => {
    if (!open) return undefined
    const dismissIfNeeded = (event) => {
      if (shouldDismissLayer(rootRef.current, event)) onDismissRef.current()
    }
    document.addEventListener('pointerdown', dismissIfNeeded)
    document.addEventListener('keydown', dismissIfNeeded)
    return () => {
      document.removeEventListener('pointerdown', dismissIfNeeded)
      document.removeEventListener('keydown', dismissIfNeeded)
    }
  }, [open])

  return rootRef
}
