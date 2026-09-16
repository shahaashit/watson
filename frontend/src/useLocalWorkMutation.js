import { useEffect, useRef, useState } from 'react'
import { syncMonitor } from './useSyncRefresh.js'
import { localWorkFailureMessage, runLocalWorkMutation } from './localWorkMutation.js'

export default function useLocalWorkMutation(scope, onChanged) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const pending = useRef(false)
  const generation = useRef(0)
  useEffect(() => {
    generation.current += 1
    pending.current = false
    setBusy(false); setError('')
    return () => { generation.current += 1 }
  }, [scope])

  const run = async (action, failureMessage) => {
    if (pending.current) return false
    pending.current = true
    const current = generation.current
    setBusy(true); setError('')
    try {
      const detail = await runLocalWorkMutation(action, syncMonitor)
      if (current === generation.current) { onChanged?.(detail); return true }
      return false
    } catch (error) {
      if (current === generation.current) setError(localWorkFailureMessage(error, failureMessage))
      return false
    } finally {
      if (current === generation.current) { pending.current = false; setBusy(false) }
    }
  }
  return { busy, error, run }
}
