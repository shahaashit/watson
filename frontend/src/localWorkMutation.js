import { ApiError } from './api.js'

export function localWorkFailureMessage(error, fallback) {
  return error instanceof ApiError && error.status === 409 && error.detail?.trim()
    ? error.detail : fallback
}

// These actions only mutate Watson's local records. Never start provider sync.
export async function runLocalWorkMutation(action, monitor) {
  const release = monitor.hold()
  try {
    const response = await action()
    monitor.refresh()
    return response.work_item
  } finally {
    release()
  }
}
