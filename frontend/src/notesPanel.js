const NOTES_OPEN_STORAGE_KEY = 'watson.notes.open'

export function readNotesOpen(storage = typeof localStorage === 'undefined' ? null : localStorage) {
  try { return storage?.getItem(NOTES_OPEN_STORAGE_KEY) === 'true' }
  catch { return false }
}

export function writeNotesOpen(open, storage = typeof localStorage === 'undefined' ? null : localStorage) {
  try { storage?.setItem(NOTES_OPEN_STORAGE_KEY, open ? 'true' : 'false') }
  catch { /* private browsing or unavailable storage */ }
}
