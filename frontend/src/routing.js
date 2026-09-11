const SETTINGS_SECTIONS = new Set(['profile', 'integrations', 'people', 'sync', 'data'])
const WORK_ID_PATTERN = /^[1-9][0-9]{0,14}$/

export function parseRoute(pathname) {
  const path = pathname.replace(/\/+$/, '') || '/'

  if (path === '/' || path === '/home' || path === '/team') return { view: 'home' }
  if (path === '/log') return { view: 'log' }
  if (path === '/onboarding') return { view: 'onboarding' }
  if (path === '/settings') return { view: 'settings' }

  const workMatch = path.match(/^\/work\/(.+)$/)
  if (workMatch && WORK_ID_PATTERN.test(workMatch[1])) {
    return { view: 'work-detail', workItemId: Number(workMatch[1]) }
  }

  const settingsMatch = path.match(/^\/settings\/([^/]+)$/)
  if (settingsMatch && SETTINGS_SECTIONS.has(settingsMatch[1])) {
    return { view: 'settings', section: settingsMatch[1] }
  }

  return { view: 'home' }
}

export function navigate(path, state = {}) {
  window.history.pushState(state, '', path)
  window.dispatchEvent(new Event('watson:route'))
}

export function subscribeRoute(listener) {
  const notify = () => listener(parseRoute(window.location.pathname))
  window.addEventListener('watson:route', notify)
  window.addEventListener('popstate', notify)
  return () => {
    window.removeEventListener('watson:route', notify)
    window.removeEventListener('popstate', notify)
  }
}
