import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { withPermanentLanes } from '../src/teamLanes.js'

test('work surfaces keep local state out of the interface', () => {
  const myWork = fs.readFileSync(new URL('../src/views/MyWork.jsx', import.meta.url), 'utf8')
  const detail = fs.readFileSync(new URL('../src/views/WorkDetail.jsx', import.meta.url), 'utf8')
  const profile = fs.readFileSync(new URL('../src/components/ProfileSettings.jsx', import.meta.url), 'utf8')
  assert.doesNotMatch(myWork, /title="Today"|title="Next"|title="Waiting"|title="Done today"/)
  assert.doesNotMatch(detail, /Local state|Local priority|detail-state/)
  assert.doesNotMatch(profile, /Separate by status|Today, Next, Waiting, and Done Today/)
})

test('work capture and read-only external import are reachable', () => {
  const apiSource = fs.readFileSync(new URL('../src/api.js', import.meta.url), 'utf8')
  const myWork = fs.readFileSync(new URL('../src/views/MyWork.jsx', import.meta.url), 'utf8')
  const detail = fs.readFileSync(new URL('../src/views/WorkDetail.jsx', import.meta.url), 'utf8')
  const verifier = fs.readFileSync(new URL('../../scripts/verify-ui.py', import.meta.url), 'utf8')
  for (const token of ['workItemId', 'work_item_id']) assert.match(apiSource, new RegExp(token))
  for (const token of ['Capture to this work', 'api.capture', 'detail.id']) assert.match(detail, new RegExp(token.replace('.', '\\.')))
  for (const token of ['Import link', 'GitLab MR or ClickUp task URL', 'api.importWorkUrl', 'sourceBoard']) assert.match(myWork, new RegExp(token.replace('.', '\\.')))
  for (const token of ['Capture to this work', 'Import link']) assert.match(verifier, new RegExp(token))
})

test('work lists keep native drag ordering affordances', () => {
  const source = fs.readFileSync(new URL('../src/components/WorkList.jsx', import.meta.url), 'utf8')
  const card = fs.readFileSync(new URL('../src/components/WorkCard.jsx', import.meta.url), 'utf8')
  const board = fs.readFileSync(new URL('../src/views/MyWork.jsx', import.meta.url), 'utf8')
  for (const token of ['onDragOver', 'onDrop', 'is-drop-target-before']) assert.match(source, new RegExp(token))
  for (const token of ['draggable', 'onDragStart']) assert.match(source + card, new RegExp(token))
  for (const token of ['api.moveWork', 'moveBoardCard']) assert.match(board, new RegExp(token.replace('.', '\\.')))
})

test('My Work reloads its calendar through the shared cache refresh', () => {
  const source = fs.readFileSync(new URL('../src/views/MyWork.jsx', import.meta.url), 'utf8')
  assert.match(source, /api\.today/)
  assert.match(source, /loadCalendar/)
  assert.match(source, /useCacheRefresh/)
})

test('integration health displays safe cached state metadata', () => {
  const source = fs.readFileSync(new URL('../src/components/IntegrationHealth.jsx', import.meta.url), 'utf8')
  for (const token of ['unconfigured', 'source.message', 'cached_age_seconds', 'retry_at', 'Retry now', 'Open Settings']) {
    assert.match(source, new RegExp(token.replace('.', '\\.')))
  }
  assert.doesNotMatch(source, /technical_error/)
  assert.match(source, /review-automation/)
  assert.match(source, /Review automation/)
  assert.match(source, /deferred.*failed.*uncertain/s)
})

test('Team and Work Detail contain approved structures and safe external links', () => {
  const team = fs.readFileSync(new URL('../src/views/Team.jsx', import.meta.url), 'utf8')
  const teamLanes = fs.readFileSync(new URL('../src/teamLanes.js', import.meta.url), 'utf8')
  const lane = fs.readFileSync(new URL('../src/components/PersonLane.jsx', import.meta.url), 'utf8')
  const teamCss = fs.readFileSync(new URL('../src/styles/team.css', import.meta.url), 'utf8')
  const detail = fs.readFileSync(new URL('../src/views/WorkDetail.jsx', import.meta.url), 'utf8')
  const linked = fs.readFileSync(new URL('../src/components/LinkedWork.jsx', import.meta.url), 'utf8')
  const followups = fs.readFileSync(new URL('../src/components/WorkFollowups.jsx', import.meta.url), 'utf8')
  const activity = fs.readFileSync(new URL('../src/components/ActivityThread.jsx', import.meta.url), 'utf8')
  for (const token of ['Others', 'Unassigned', 'api.teamWork', 'aria-live']) {
    assert.match(team + teamLanes + lane, new RegExp(token.replace('.', '\\.')))
  }
  for (const token of ['overflow-y: auto', 'scrollbar-gutter: stable', 'onMouseDown', 'onMouseMove', 'onMouseUp']) {
    assert.match(token.startsWith('overflow') || token.startsWith('scrollbar') ? teamCss : lane, new RegExp(token.replace('.', '\\.')))
  }
  assert.match(teamCss, /repeat\(4/)
  assert.match(teamCss, /@media \(max-width: 1100px\).*repeat\(3/)
  assert.match(teamCss, /height: 322px; min-height: 322px/)
  for (const [token, source] of [['Activity', activity], ['Linked work', linked], ['Follow-ups', followups], ['Pending actions', detail], ['api.addWorkActivity', detail], ['PendingAction', detail]]) {
    assert.match(source, new RegExp(token.replace('.', '\\.')))
  }
  assert.match(linked, /target="_blank"/)
  assert.match(linked, /noopener noreferrer/)
  assert.match(followups, /api\.reminderDone/)
  assert.match(followups, /api\.reminderSnooze/)
})

test('Team lanes retain tracked people with reserved display names', () => {
  const trackedOthers = { name: 'Others', person: { id: 41, display_name: 'Others' }, items: [{ id: 1 }] }
  const trackedUnassigned = { name: 'Unassigned', person: { id: 42, display_name: 'Unassigned' }, items: [{ id: 2 }] }
  const lanes = withPermanentLanes([trackedOthers, trackedUnassigned])

  assert.deepEqual(lanes.map((lane) => [lane.name, lane.person?.id || null]), [
    ['Others', 41], ['Unassigned', 42], ['Others', null], ['Unassigned', null],
  ])
})

test('Settings communicates permanent editing and local credential storage', () => {
  const settings = fs.readFileSync(new URL('../src/views/Settings.jsx', import.meta.url), 'utf8')
  const onboarding = fs.readFileSync(new URL('../src/views/Onboarding.jsx', import.meta.url), 'utf8')
  for (const token of ['Integrations', 'People', 'Disconnect', 'Sync', 'Data & Backup', 'AI provider']) {
    assert.match(settings, new RegExp(token))
  }
  assert.match(onboarding, /macOS Keychain/)
  assert.match(onboarding, /Continue to People/)
})

test('Settings keeps secrets write-only and exposes the real source controls', () => {
  const integrations = fs.readFileSync(new URL('../src/components/IntegrationSettings.jsx', import.meta.url), 'utf8')
  const people = fs.readFileSync(new URL('../src/components/PeopleSettings.jsx', import.meta.url), 'utf8')
  const syncData = fs.readFileSync(new URL('../src/components/SyncDataSettings.jsx', import.meta.url), 'utf8')
  for (const token of ['anthropic', 'gitlab', 'clickup', 'google-calendar', 'credential_present', 'macOS Keychain', 'Disconnect']) {
    assert.match(integrations, new RegExp(token.replace('.', '\\.')))
  }
  assert.doesNotMatch(integrations, /localStorage|sessionStorage/)
  for (const token of ['identifier', 'Others', 'lane_position', 'Only people you add here']) {
    assert.match(people, new RegExp(token))
  }
  assert.doesNotMatch(people, /toggleTracked|Track this person in Team|Try untracking/)
  assert.match(people, /Identifier/)
  assert.match(people, /profile email domain/)
  assert.doesNotMatch(people, /@media\.net/)
  assert.doesNotMatch(people, /Source identities|Add identity|IDENTITY_SOURCES/)
  for (const token of ['backup_dir', 'live SQLite', 'api.retrySync']) {
    assert.match(syncData, new RegExp(token.replace('.', '\\.')))
  }
})

test('Sync settings reports an already-running sync honestly and guards retries', () => {
  const syncData = fs.readFileSync(new URL('../src/components/SyncDataSettings.jsx', import.meta.url), 'utf8')
  for (const token of ['skipped', 'already_running', 'useSyncStatus', 'retryingSource', 'disabled={syncing || retryingSource']) {
    assert.match(syncData, new RegExp(token.replace('.', '\\.').replace('{', '\\{')))
  }
  assert.doesNotMatch(syncData, /await api\.syncAll\(\); setMessage\('Sync finished\.'/)
})

test('permanent Sync and Data settings panels select distinct content', () => {
  const settings = fs.readFileSync(new URL('../src/views/Settings.jsx', import.meta.url), 'utf8')
  assert.match(settings, /mode=\{section\}/)
})

test('sync polling is invalidated across a settings mode change', () => {
  const syncData = fs.readFileSync(new URL('../src/components/SyncDataSettings.jsx', import.meta.url), 'utf8')
  for (const token of ['pollGenerationRef', 'generation !== pollGenerationRef.current', 'const generation = pollGenerationRef.current', 'pollGenerationRef.current += 1']) {
    assert.match(syncData, new RegExp(token.replace('.', '\\.').replace('+', '\\+')))
  }
})

test('Log exposes work context without offering deletion for work rows', () => {
  const log = fs.readFileSync(new URL('../src/views/Log.jsx', import.meta.url), 'utf8')
  for (const token of ["key: 'work'", 'Work context', 'work_activity', 'work_completed', "navigate(item.url)"]) {
    assert.match(log, new RegExp(token.replace('.', '\\.').replace('(', '\\(').replace(')', '\\)')))
  }
  assert.match(log, /item\.source === 'entry'/)
  assert.doesNotMatch(log, /state_change|State change/)
})

test('Command bar routes work locally, includes approved actions, and guards stale search', () => {
  const command = fs.readFileSync(new URL('../src/components/CommandBar.jsx', import.meta.url), 'utf8')
  for (const token of ['My Work', 'Team', 'Add Work', 'Capture', 'Ask Watson', 'Settings', 'Sync now', 'work_item', 'work_activity', 'AbortController', 'searchGenerationRef', "navigateTo(item.url)", "'noopener,noreferrer'"]) {
    assert.match(command, new RegExp(token.replace('.', '\\.').replace('(', '\\(').replace(')', '\\)')))
  }
})

test('Command bar never creates a negative selection for an empty result list', () => {
  const command = fs.readFileSync(new URL('../src/components/CommandBar.jsx', import.meta.url), 'utf8')
  assert.match(command, /if \(combined\.length === 0\) return/)
  assert.match(command, /Math\.max\(0, Math\.min\(s \+ 1, combined\.length - 1\)\)/)
})

test('Log has a guarded loading lifecycle before rendering its empty state', () => {
  const log = fs.readFileSync(new URL('../src/views/Log.jsx', import.meta.url), 'utf8')
  for (const token of ['loading', 'logGenerationRef', 'AbortController', 'setError(null)', 'generation === logGenerationRef.current', '!loading && items.length === 0']) {
    assert.match(log, new RegExp(token.replace('.', '\\.').replace('(', '\\(').replace(')', '\\)').replace('!', '\\!')))
  }
})
