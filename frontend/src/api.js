async function request(path, options = {}) {
  const resp = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!resp.ok) {
    let detail = resp.statusText
    try {
      detail = (await resp.json()).detail || detail
    } catch { /* non-JSON error body */ }
    throw new Error(detail)
  }
  return resp.json()
}

const get = (path, options = {}) => request(path, options)
const post = (path, body) => request(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined })
const patch = (path, body) => request(path, { method: 'PATCH', body: JSON.stringify(body) })
const put = (path, body) => request(path, { method: 'PUT', body: JSON.stringify(body) })
const del = (path) => request(path, { method: 'DELETE' })

export const api = {
  health: () => get('/api/health'),
  capture: (text, workItemId = null) => post('/api/capture', {
    text,
    ...(workItemId == null ? {} : { work_item_id: workItemId }),
  }),
  recentCaptures: () => get('/api/captures?limit=10'),
  entries: (params = {}) => get('/api/entries?' + new URLSearchParams(params)),
  deleteEntry: (id) => del(`/api/entries/${id}`),
  reminders: (status) => get('/api/reminders' + (status ? `?status=${status}` : '')),
  reminderDone: (id) => post(`/api/reminders/${id}/done`),
  reminderSnooze: (id, until) => post(`/api/reminders/${id}/snooze`, { until }),
  actions: (status = 'pending') => get(`/api/actions?status=${status}`),
  approveAction: (id) => post(`/api/actions/${id}/approve`),
  rejectAction: (id) => post(`/api/actions/${id}/reject`),
  bulkActions: (op, ids) => post('/api/actions/bulk', { op, ids }),
  editAction: (id, body) => patch(`/api/actions/${id}`, body),
  today: (options = {}) => get('/api/today', options),
  meetingsForDate: (date) => get(`/api/meetings?date=${encodeURIComponent(date)}`),
  dismissFlockChannel: (channelJid) => del(`/api/flock/webhook/mentions?channel_jid=${encodeURIComponent(channelJid)}`),
  syncAll: () => post('/api/sync'),
  syncStatus: (options = {}) => get('/api/sync/status', options),
  syncHealth: (options = {}) => get('/api/sync/status', options),
  retrySync: (source) => post(`/api/sync/retry/${encodeURIComponent(source)}`),
  reviewSuggestions: (options = {}) => get('/api/review-automation/suggestions', options),
  retryReviewGroup: (id) => post(`/api/review-automation/groups/${id}/retry`),
  syncClickup: () => post('/api/sync/clickup'),
  syncGitlab: () => post('/api/sync/gitlab'),
  ask: (question) => post('/api/ask', { question }),
  log: (params = {}, options = {}) => {
    const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v))
    return get('/api/log' + (q.toString() ? '?' + q : ''), options)
  },
  search: (q, options = {}) => get('/api/search?q=' + encodeURIComponent(q || ''), options),

  // Local work board. These routes never write to ClickUp or GitLab.
  myWork: (options = {}) => get('/api/work-items/my', options),
  teamWork: ({ meMode = false, ...options } = {}) => get(
    '/api/work-items/team' + (meMode ? '?me_mode=true' : ''),
    options,
  ),
  workDetail: (id, options = {}) => get(`/api/work-items/${id}`, options),
  createWork: (work) => post('/api/work-items', work),
  updateWork: (id, changes) => patch(`/api/work-items/${id}`, changes),
  moveWork: (id, placement) => patch(`/api/work-items/${id}/position`, placement),
  addWorkActivity: (id, activity) => post(`/api/work-items/${id}/activity`, activity),
  addWorkLink: (id, link) => post(`/api/work-items/${id}/links`, link),
  importWorkUrl: (url) => post('/api/work-import', { url }),
  workInbox: (options = {}) => get('/api/work-inbox', options),
  resolveInbox: (id, workItemId) => post(`/api/work-inbox/${id}/resolve`, { work_item_id: workItemId }),
  dismissInbox: (id) => post(`/api/work-inbox/${id}/dismiss`),

  // Secrets supplied here are sent directly to the local API and are never
  // persisted by this client; settings responses only expose their presence.
  settings: () => get('/api/settings'),
  updateProfile: (profile) => patch('/api/settings/profile', profile),
  saveIntegration: (source, integration) => put(`/api/settings/integrations/${encodeURIComponent(source)}`, integration),
  disconnectIntegration: (source) => del(`/api/settings/integrations/${encodeURIComponent(source)}?confirm=true`),
  importIntegrationEnv: (source) => post(`/api/settings/integrations/${encodeURIComponent(source)}/import-env`),
  testIntegration: (source) => post(`/api/settings/integrations/${encodeURIComponent(source)}/test`),
  gitlabProjects: (query = '') => get('/api/settings/integrations/gitlab/projects' + (query ? `?q=${encodeURIComponent(query)}` : '')),
  saveGitlabProjects: (projectIds) => put('/api/settings/integrations/gitlab/projects', { project_ids: projectIds }),
  connectGoogleCalendar: () => post('/api/settings/integrations/google-calendar/connect'),
  connectGitlab: () => post('/api/settings/integrations/gitlab/connect'),
  connectClickup: () => post('/api/settings/integrations/clickup/connect'),
  clickupConnectStatus: (sessionId) => get(`/api/settings/integrations/clickup/connect/${encodeURIComponent(sessionId)}`),
  clickupWorkspaces: () => get('/api/settings/integrations/clickup/workspaces'),
  clickupSpaces: (workspaceId) => get(`/api/settings/integrations/clickup/workspaces/${encodeURIComponent(workspaceId)}/spaces`),
  clickupLists: (workspaceId, spaceId) => get(`/api/settings/integrations/clickup/workspaces/${encodeURIComponent(workspaceId)}/spaces/${encodeURIComponent(spaceId)}/lists`),
  saveClickupDestination: (destination) => post('/api/settings/integrations/clickup/destination', destination),
  gitlabConnectStatus: (sessionId) => get(`/api/settings/integrations/gitlab/connect/${encodeURIComponent(sessionId)}`),
  googleCalendarConnectStatus: (sessionId) => get(`/api/settings/integrations/google-calendar/connect/${encodeURIComponent(sessionId)}`),
  people: (options = {}) => get('/api/settings/people', options),
  savePerson: (person, personId) => personId == null
    ? post('/api/settings/people', person)
    : patch(`/api/settings/people/${personId}`, person),
  editPerson: (personId, person) => patch(`/api/settings/people/${personId}`, person),
  deletePerson: (personId) => del(`/api/settings/people/${personId}`),
  onboarding: () => get('/api/onboarding'),
  updateOnboarding: (state) => patch('/api/onboarding', state),
  updateDataSettings: (data) => patch('/api/settings/data', data),
}
