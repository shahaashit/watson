import { useEffect, useState } from 'react'
import { api } from '../api.js'
import ReviewAutomationSettings from './ReviewAutomationSettings.jsx'

function safeError() {
  return 'Could not save your profile. Check the values and try again.'
}

export default function ProfileSettings({ profile, onSaved, compact = false }) {
  const [displayName, setDisplayName] = useState(profile?.display_name || '')
  const [timezone, setTimezone] = useState(profile?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC')
  const [emailDomain, setEmailDomain] = useState(profile?.email_domain || 'example.com')
  const [reviewAutomation, setReviewAutomation] = useState({
    auto_create_review_tasks: profile?.auto_create_review_tasks !== false,
    ai_group_review_mrs: profile?.ai_group_review_mrs !== false,
  })
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')

  useEffect(() => {
    setDisplayName(profile?.display_name || '')
    setTimezone(profile?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC')
    setEmailDomain(profile?.email_domain || 'example.com')
    setReviewAutomation({
      auto_create_review_tasks: profile?.auto_create_review_tasks !== false,
      ai_group_review_mrs: profile?.ai_group_review_mrs !== false,
    })
  }, [profile?.display_name, profile?.timezone, profile?.email_domain, profile?.auto_create_review_tasks, profile?.ai_group_review_mrs])

  const save = async (event) => {
    event.preventDefault()
    if (busy || !displayName.trim() || !timezone.trim() || !emailDomain.trim()) return
    setBusy(true); setMessage('')
    try {
      const response = await api.updateProfile({
        display_name: displayName.trim(),
        timezone: timezone.trim(),
        email_domain: emailDomain.trim(),
        auto_create_review_tasks: reviewAutomation.auto_create_review_tasks,
        ai_group_review_mrs: reviewAutomation.ai_group_review_mrs,
      })
      setMessage('Profile saved locally.')
      onSaved?.(response.profile)
    } catch {
      setMessage(safeError())
    } finally { setBusy(false) }
  }

  return <section className={`settings-section profile-settings${compact ? ' settings-compact' : ''}`} aria-labelledby="profile-settings-title">
    <div className="settings-section-heading"><div><p className="eyebrow">Profile</p><h2 id="profile-settings-title">Your Watson profile</h2></div></div>
    <form className="settings-form" onSubmit={save}>
      <label>Display name<input value={displayName} onChange={(event) => setDisplayName(event.target.value)} autoComplete="name" disabled={busy} required /></label>
      <label>Timezone<input value={timezone} onChange={(event) => setTimezone(event.target.value)} placeholder="UTC" disabled={busy} required /></label>
      <label>Email domain<input value={emailDomain} onChange={(event) => setEmailDomain(event.target.value)} placeholder="example.com" disabled={busy} required /><span className="settings-muted">Used for ClickUp and Calendar identities.</span></label>
      <ReviewAutomationSettings values={reviewAutomation} onChange={setReviewAutomation} disabled={busy} />
      <div className="settings-form-actions"><button className="settings-primary" type="submit" disabled={busy}>{busy ? 'Saving…' : 'Save profile'}</button>{message && <p className={message.includes('Could not') ? 'settings-error' : 'settings-success'} role="status">{message}</p>}</div>
    </form>
  </section>
}
