export function attachBoardRefreshGuard(target, windowTarget, hold) {
  let release
  let nativeDrag = false
  const begin = event => {
    if (event.target.closest?.('.person-lane-card, .work-card-wrap')) release ||= hold()
  }
  const finish = () => { nativeDrag = false; release?.(); release = null }
  const finishPointer = () => { if (!nativeDrag) finish() }
  const dragStart = event => { begin(event); nativeDrag = Boolean(release) }
  // Native HTML dragging cancels the pointer stream; only dragend ends that hold.
  const events = { pointerdown: begin, mouseup: finishPointer, pointercancel: finishPointer, dragstart: dragStart, dragend: finish }
  Object.entries(events).forEach(([type, listener]) => target.addEventListener(type, listener))
  windowTarget.addEventListener('blur', finish)
  return () => {
    finish()
    Object.entries(events).forEach(([type, listener]) => target.removeEventListener(type, listener))
    windowTarget.removeEventListener('blur', finish)
  }
}
