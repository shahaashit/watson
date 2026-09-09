const RESERVED_LANES = ['Others', 'Unassigned']
const ME_MODE_STORAGE_KEY = 'watson.team.me-mode'

export function readMeMode(storage = typeof localStorage === 'undefined' ? null : localStorage) {
  try { return storage?.getItem(ME_MODE_STORAGE_KEY) === 'true' }
  catch { return false }
}

export function writeMeMode(enabled, storage = typeof localStorage === 'undefined' ? null : localStorage) {
  try { storage?.setItem(ME_MODE_STORAGE_KEY, enabled ? 'true' : 'false') }
  catch { /* private browsing or unavailable storage */ }
}

function isPermanentLane(lane, name) {
  return lane?.person == null && lane?.name === name
}

// Backend supplies tracked people in order. A display name is allowed to match
// a reserved pseudo-lane name, so only a non-person lane is special here.
export function withPermanentLanes(lanes) {
  const source = Array.isArray(lanes) ? lanes : []
  const tracked = source.filter((lane) => !RESERVED_LANES.some((name) => isPermanentLane(lane, name)))
  const permanent = RESERVED_LANES.map((name) => source.find((lane) => isPermanentLane(lane, name)) || {
    name,
    person: null,
    items: [],
  })
  return [...tracked, ...permanent]
}
