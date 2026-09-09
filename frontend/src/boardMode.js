export const BOARD_MODE_FLAT = 'flat'
export const BOARD_MODE_SEGREGATED = 'segregated'

export function boardModeFromProfile(profile) {
  return profile?.separate_work_by_status ? BOARD_MODE_SEGREGATED : BOARD_MODE_FLAT
}

export function profileBoardPreference(mode) {
  return { separate_work_by_status: mode === BOARD_MODE_SEGREGATED }
}
