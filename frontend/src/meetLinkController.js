import { ApiError } from './api.js'

const STORAGE_KEY = 'watson.meet-link-request'
const ERROR_MESSAGES = {
  reconnect_required: 'This older request failed. Try generating a new link.',
  api_disabled: 'This older request failed. Try generating a new link.',
  permission_denied: 'This older request failed. Try generating a new link.',
  in_progress: 'This request is still in progress. Retry to check the same request.',
  uncertain: 'The result is not confirmed. Retry uses the same request ID to avoid creating a duplicate.',
  failed: 'Could not generate this Meet link. Please try again.',
}

function meetingUrl(value) {
  try {
    const url = new URL(value)
    if (url.origin === 'https://g.co' && !url.username && !url.password && !url.search && !url.hash
      && /^\/meet\/w-(?:[0-9a-f]{4}-){3}[0-9a-f]{4}$/.test(url.pathname)) return url.href
    return url.origin === 'https://meet.google.com' && !url.username && !url.password
      && /^\/[a-z]{3}-[a-z]{4}-[a-z]{3}\/?$/.test(url.pathname) ? url.href : ''
  } catch { return '' }
}

// The store survives view unmounts. Session storage also retains uncertain IDs
// across reloads; merely mounting/subscribing never creates or retries a Meet.
export function createMeetLinkController({ create, copy, uuid, storage }) {
  let saved
  try { saved = JSON.parse(storage?.getItem(STORAGE_KEY) || 'null') } catch { /* Storage can be unavailable. */ }
  let state = {
    busy: false, copied: false, code: '', message: '',
    requestId: !saved?.url && typeof saved?.requestId === 'string' ? saved.requestId : '',
    url: '',
  }
  let confirmationTimer
  const listeners = new Set()
  const update = values => {
    state = { ...state, ...values }
    listeners.forEach(listener => listener())
  }
  const persist = () => {
    try { storage?.setItem(STORAGE_KEY, JSON.stringify({ requestId: state.url ? '' : state.requestId })) } catch { /* In-memory ID still protects retries. */ }
  }
  const copyUrl = async () => {
    try {
      await copy(state.url)
      update({ copied: true, message: 'Link copied', url: '', requestId: '' })
      persist()
      clearTimeout(confirmationTimer)
      confirmationTimer = setTimeout(() => update({ copied: false, message: '' }), 3000)
    } catch {
      update({ copied: false, message: 'Link created. Clipboard access was blocked; copy the link below.' })
    }
  }
  return {
    subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener) },
    getSnapshot: () => state,
    dismiss: () => {
      if (state.busy || !state.url) return
      update({ url: '', requestId: '', message: '', code: '', copied: false })
      persist()
    },
    start: async () => {
      if (state.busy) return
      clearTimeout(confirmationTimer)
      update({ busy: true, copied: false, code: '', message: '' })
      try {
        const requestId = state.url ? uuid() : state.requestId || uuid()
        update({ requestId, url: '' })
        persist()
        const response = await create(requestId)
        const url = meetingUrl(response.url)
        if (!url) throw new Error('Invalid meeting response')
        update({ url })
        persist()
        if (listeners.size) await copyUrl()
        else update({ message: 'Link created. Copy it below.' })
      } catch (error) {
        const code = error instanceof ApiError && Object.hasOwn(ERROR_MESSAGES, error.code) ? error.code : 'uncertain'
        update({ code, message: ERROR_MESSAGES[code] })
        if (['reconnect_required', 'api_disabled', 'permission_denied', 'failed'].includes(code)) {
          // These are confirmed failures; replaying their ID only replays the error.
          update({ requestId: '' })
          persist()
        }
      } finally { update({ busy: false }) }
    },
    copyAgain: async () => {
      if (state.busy || !state.url) return
      update({ busy: true })
      try { await copyUrl() } finally { update({ busy: false }) }
    },
  }
}
