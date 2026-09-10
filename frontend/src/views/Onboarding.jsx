import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { navigate } from '../routing.js'
import ProfileSettings from '../components/ProfileSettings.jsx'
import IntegrationSettings from '../components/IntegrationSettings.jsx'
import PeopleSettings from '../components/PeopleSettings.jsx'
import SyncDataSettings from '../components/SyncDataSettings.jsx'

const STEPS = ['Profile', 'Integrations', 'People', 'Ready']

export default function Onboarding({ initialState, onComplete }) {
  const [snapshot, setSnapshot] = useState(null)
  const [step, setStep] = useState(Math.min(4, Math.max(1, initialState?.step || 1)))
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [moving, setMoving] = useState(false)

  const load = useCallback(async () => {
    setLoading(true); setError('')
    try { setSnapshot(await api.settings()) }
    catch { setError('Setup details are temporarily unavailable. Retry to continue; manual local work is still safe.') }
    finally { setLoading(false) }
  }, [])
  useEffect(() => { load() }, [load])
  useEffect(() => { if (initialState?.step) setStep(Math.min(4, Math.max(1, initialState.step))) }, [initialState?.step])

  const move = async (target) => {
    if (moving || target < 1 || target > 4) return
    setMoving(true); setError('')
    try { const state = await api.updateOnboarding({ step: target }); setStep(state.step) }
    catch { setError('Could not save setup progress. Try again.') }
    finally { setMoving(false) }
  }
  const finish = async () => {
    if (moving) return
    setMoving(true); setError('')
    try {
      const state = await api.updateOnboarding({ completed: true, step: 4 })
      onComplete?.(state)
      navigate('/my-work')
    } catch { setError('Could not finish setup. Your settings are still saved; retry when ready.') }
    finally { setMoving(false) }
  }

  const current = () => {
    if (loading) return <p className="settings-muted">Loading setup…</p>
    if (error && !snapshot) return <div className="settings-recoverable-error" role="alert"><p>{error}</p><button type="button" onClick={load}>Retry setup</button></div>
    if (step === 1) return <ProfileSettings profile={{
      ...snapshot?.profile,
      auto_create_review_tasks: snapshot?.profile?.auto_create_review_tasks !== false,
      ai_group_review_mrs: snapshot?.profile?.ai_group_review_mrs !== false,
    }} compact onSaved={async (profile) => {
      setSnapshot((value) => ({ ...value, profile }))
      await move(2)
    }} />
    if (step === 2) return <IntegrationSettings integrations={snapshot?.integrations} compact onChanged={load} />
    if (step === 3) return <PeopleSettings compact />
    return <section className="onboarding-ready"><p className="eyebrow">Ready</p><h2>Your workspace is ready.</h2><p>Start with a note or a task. Connect more services whenever you’re ready.</p><SyncDataSettings data={snapshot?.data} compact /></section>
  }

  const nextLabel = step === 1 ? 'Continue to Integrations' : step === 2 ? 'Continue to People' : step === 3 ? 'Continue to Ready' : 'Start using Watson'
return <div className="onboarding-page"><header className="onboarding-header"><div className="onboarding-brand" aria-hidden="true">W</div><div><p className="eyebrow">Welcome to Watson</p><h1>A little setup. A clearer workday.</h1><p>Make space for your work, notes and team. Connections are optional; credentials stay secure in macOS Keychain.</p></div></header><ol className="onboarding-steps" aria-label="Onboarding progress">{STEPS.map((label, index) => <li key={label} className={index + 1 === step ? 'active' : index + 1 < step ? 'complete' : ''}><span>{index + 1}</span>{label}</li>)}</ol>{error && <p className="settings-error" role="alert">{error}</p>}<div className="onboarding-content">{current()}</div><footer className="onboarding-footer">{step > 1 ? <button type="button" onClick={() => move(step - 1)} disabled={moving}>Back</button> : <span />}{step === 4 ? <button className="settings-primary" type="button" onClick={finish} disabled={moving}>{moving ? 'Finishing…' : nextLabel}</button> : step > 1 && <button className="settings-primary" type="button" onClick={() => move(step + 1)} disabled={moving}>{moving ? 'Saving…' : nextLabel}</button>}</footer></div>
}
