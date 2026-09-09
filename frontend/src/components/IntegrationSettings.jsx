import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import GitLabProjectPicker from './GitLabProjectPicker.jsx'
import GitLabConnect from './GitLabConnect.jsx'
import ClickUpConnect from './ClickUpConnect.jsx'
import ClickUpDestination from './ClickUpDestination.jsx'

const INTEGRATIONS = [
  { source: 'anthropic', title: 'AI provider', hint: 'Capture classification and Ask need an AI provider.', secret: 'api_key', secretLabel: 'API key', fields: [['base_url', 'Base URL'], ['model', 'Model']] },
  { source: 'gitlab', title: 'GitLab', hint: 'Watson collects merge-request context into its local cache.', secret: 'token', secretLabel: 'Access token', fields: [['base_url', 'Base URL'], ['username', 'Username (optional)']] },
  { source: 'clickup', title: 'ClickUp', hint: 'Exact branch task IDs enrich local work without broad task scans.', secret: 'token', secretLabel: 'API token', fields: [['create_list_id', 'Create-list ID']] },
  { source: 'google-calendar', title: 'Google Calendar', hint: 'Calendar stays optional; local work is still available without it.', secret: 'client_config_json', secretLabel: 'OAuth client configuration JSON', fields: [] },
]

const SOURCE_LABEL = { ...Object.fromEntries(INTEGRATIONS.map(({ source, title }) => [source, title])), 'review-automation': 'Review automation' }
const FASTROUTER_URL = 'https://go.fastrouter.ai'
const DEFAULT_AI_MODEL = 'claude-sonnet-4-6'

function isFastRouter(baseUrl) {
  try {
    const url = new URL(baseUrl)
    return url.protocol === 'https:' && ['go.fastrouter.ai', 'api.fastrouter.ai'].includes(url.hostname)
  } catch { return false }
}

function safeError() {
  return 'That action could not be completed. Review the local configuration and try again.'
}

function configFor(source, integration) {
  const fields = INTEGRATIONS.find((item) => item.source === source)?.fields || []
  const values = Object.fromEntries(fields.map(([key]) => [key, integration?.[key] || '']))
  if (source === 'anthropic') {
    if (!values.base_url && !integration?.configured && !integration?.credential_present) values.base_url = FASTROUTER_URL
    if (!values.model) values.model = DEFAULT_AI_MODEL
  }
  return values
}

function stateLabel(integration) {
  if (!integration?.configured) return 'Not configured'
  if (integration.health?.status === 'failed') return 'Needs attention'
  if (integration.health?.status === 'connected') return 'Connected'
  return 'Configured'
}

function GitLabProjectSettings() {
  const [projects, setProjects] = useState([])
  const [selectedIds, setSelectedIds] = useState([])
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState('load')
  const [message, setMessage] = useState('')

  const load = async () => {
    setBusy('load'); setMessage('')
    try {
      const data = await api.gitlabProjects()
      setProjects(data.projects || [])
      setSelectedIds((data.selected || []).map((project) => project.id))
    } catch { setMessage('Repositories could not be loaded from GitLab.') }
    finally { setBusy('') }
  }
  useEffect(() => { load() }, [])

  const toggle = (projectId) => setSelectedIds((current) => current.includes(projectId)
    ? current.filter((id) => id !== projectId)
    : [...current, projectId])
  const search = async () => {
    if (busy || !query.trim()) return
    setBusy('search'); setMessage('')
    try {
      const data = await api.gitlabProjects(query.trim())
      setProjects((current) => {
        const merged = new Map(current.map((project) => [project.id, project]))
        for (const project of data.projects || []) merged.set(project.id, project)
        return [...merged.values()].sort((left, right) => left.path.localeCompare(right.path))
      })
    } catch { setMessage('Repositories could not be searched in GitLab.') }
    finally { setBusy('') }
  }
  const save = async () => {
    if (busy) return
    setBusy('save'); setMessage('')
    try {
      const saved = await api.saveGitlabProjects(selectedIds)
      setSelectedIds((saved.selected || []).map((project) => project.id))
      const sync = await api.syncAll()
      setMessage(sync.skipped === 'already_running'
        ? 'Repositories saved. The running sync will apply them shortly.'
        : 'Repositories saved and Watson data refreshed.')
    } catch { setMessage('Repositories were saved or refreshed incompletely. Retry to confirm the current selection.') }
    finally { setBusy('') }
  }

  return <GitLabProjectPicker projects={projects} selectedIds={selectedIds} query={query} onQueryChange={setQuery} onSearch={search} onToggle={toggle} onSave={save} busy={busy} message={message} />
}

function IntegrationCard({ definition, integration, onChanged }) {
  const { source, title, hint, fields, secret, secretLabel } = definition
  const [values, setValues] = useState(() => configFor(source, integration))
  const [secretValue, setSecretValue] = useState('')
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')
  const [confirmDisconnect, setConfirmDisconnect] = useState(false)
  const [oauth, setOauth] = useState(null)
  const timerRef = useRef(null)

  useEffect(() => {
    setValues(configFor(source, integration))
    // Secrets are intentionally write-only. A refresh always clears this field.
    setSecretValue('')
  }, [source, integration])

  useEffect(() => () => { if (timerRef.current) window.clearTimeout(timerRef.current) }, [])

  useEffect(() => {
    if (!oauth?.sessionId) return undefined
    const deadline = oauth.startedAt + 120000
    let disposed = false
    const poll = async () => {
      try {
        const status = await api.googleCalendarConnectStatus(oauth.sessionId)
        if (disposed) return
        if (status.status === 'connected') {
          setMessage('Google Calendar connected.')
          setOauth(null); setBusy(''); onChanged?.()
          return
        }
        if (status.status === 'failed') {
          setMessage('Google Calendar could not connect. Check the client configuration and try again.')
          setOauth(null); setBusy('')
          return
        }
        if (Date.now() >= deadline) {
          setMessage('Google Calendar is still waiting. You can finish the local authorization, then reconnect.')
          setOauth(null); setBusy('')
          return
        }
        timerRef.current = window.setTimeout(poll, 1200)
      } catch {
        if (!disposed) { setMessage('Could not check the Google Calendar connection. Try reconnecting.'); setOauth(null); setBusy('') }
      }
    }
    timerRef.current = window.setTimeout(poll, 800)
    return () => { disposed = true; if (timerRef.current) window.clearTimeout(timerRef.current) }
  }, [oauth, onChanged])

  const save = async (event) => {
    event.preventDefault()
    if (busy) return
    if (source === 'anthropic' && (!integration?.credential_present || values.base_url !== (integration?.base_url || '')) && !secretValue.trim()) {
      setMessage('Enter a valid API key before saving a new connection or changing the endpoint.')
      return
    }
    setBusy('save'); setMessage('')
    const payload = { ...values }
    if (secret && secretValue.trim()) payload[secret] = secretValue.trim()
    try {
      await api.saveIntegration(source, payload)
      setSecretValue(''); setMessage(`${title} saved. Credentials remain in macOS Keychain.`); onChanged?.()
    } catch { setMessage(safeError()) }
    finally { setBusy('') }
  }
  const test = async () => {
    if (busy) return
    setBusy('test'); setMessage('')
    try {
      const response = await api.testIntegration(source)
      setMessage(response.health?.status === 'connected' ? `${title} connection confirmed.` : 'Connection test needs attention. Review the configuration and try again.')
      onChanged?.()
    } catch { setMessage('Connection test could not run. Save valid local settings, then try again.') }
    finally { setBusy('') }
  }
  const disconnect = async () => {
    if (busy) return
    setBusy('disconnect'); setMessage('')
    try { await api.disconnectIntegration(source); setSecretValue(''); setConfirmDisconnect(false); setMessage(`${title} disconnected. Future sync has stopped.`); onChanged?.() }
    catch { setMessage(safeError()) }
    finally { setBusy('') }
  }
  const connectGoogle = async () => {
    if (busy || oauth) return
    setBusy('oauth'); setMessage('')
    try {
      const response = await api.connectGoogleCalendar()
      setOauth({ sessionId: response.session_id, startedAt: Date.now() })
      setMessage('Finish authorization in the local browser window. Watson will check the result for up to two minutes.')
    } catch { setMessage('Could not start Google authorization. Check your setup file and try again.'); setBusy('') }
  }
  const actionBusy = Boolean(busy)
  const aiProvider = source === 'anthropic'
  const connectionFields = fields.map(([key, label]) => <label key={key}>{label}<input value={values[key] || ''} onChange={(event) => setValues((current) => ({ ...current, [key]: event.target.value }))} disabled={actionBusy} /></label>)
  const credentialField = secret && <>
    <label>{aiProvider && integration?.credential_present ? 'Replace API key' : secretLabel}<textarea value={secretValue} onChange={(event) => setSecretValue(event.target.value)} placeholder={integration?.credential_present ? 'Stored credential present — enter a replacement only' : 'Enter once to save locally'} disabled={actionBusy} rows={source === 'google-calendar' ? 4 : 1} aria-describedby={`${source}-credential-note`} required={aiProvider && (!integration?.credential_present || values.base_url !== (integration?.base_url || ''))} /></label>
    <p className="settings-secret-note" id={`${source}-credential-note`}>{integration?.credential_present ? (aiProvider ? 'A credential is already present. Leave blank to keep it, or paste a replacement and save. Changing the endpoint requires a new key.' : 'A credential is already present. This write-only field is blank until you choose to rotate it.') : 'This write-only value is stored locally in macOS Keychain.'}</p>
  </>

  return <article className="integration-settings-card" data-provider={source}>
    <header><div><h3 aria-label={title}>{title}</h3><p>{hint}</p></div><span className={`integration-state ${integration?.configured ? 'configured' : ''}`}>{stateLabel(integration)}</span></header>
    {!aiProvider && <div className="oauth-connection-section">
    {source === 'gitlab' && integration?.oauth_available && <>
      <p>Application setup is ready for {integration.base_url}.</p>
      <GitLabConnect connected={integration.oauth_connected} onConnected={onChanged} />
    </>}
    {source === 'google-calendar' && integration?.credential_present && <p>Application setup is ready. Use Connect Google to authorize your own account.</p>}
    {source === 'clickup' && integration?.oauth_available && <>
      <p>Connect your ClickUp account, then choose where Watson should create tasks.</p>
      <ClickUpConnect connected={integration.oauth_connected} onConnected={onChanged} />
    </>}
    {!(source === 'google-calendar' ? integration?.credential_present : integration?.oauth_available) && <p className="integration-setup-note">Ask your administrator for the private Watson setup file and import it using the installer. Then sign in here with your own account.</p>}
    {source === 'clickup' && integration?.configured && !integration?.oauth_connected && <p className="settings-secret-note">Your existing connection is still active. Connect ClickUp to switch to browser sign-in.</p>}
    <div className="integration-actions">
      {source === 'google-calendar' && <button className="settings-primary" type="button" onClick={connectGoogle} disabled={actionBusy || Boolean(oauth) || !integration?.credential_present}>{oauth ? 'Waiting for Google…' : integration?.configured ? 'Reconnect Google' : 'Connect Google'}</button>}
      {integration?.configured && <button type="button" onClick={test} disabled={actionBusy}>{busy === 'test' ? 'Testing…' : 'Test connection'}</button>}
      {(integration?.configured || integration?.oauth_connected) && <button className="settings-danger" type="button" onClick={() => setConfirmDisconnect(true)} disabled={actionBusy}>Disconnect</button>}
    </div>
    </div>}
    {aiProvider && <form className="settings-form integration-form" onSubmit={save}>
        <label>Provider<select aria-label="Provider" value={isFastRouter(values.base_url) ? 'fastrouter' : 'custom'} disabled={actionBusy} onChange={() => {
          setValues({ base_url: FASTROUTER_URL, model: DEFAULT_AI_MODEL }); setSecretValue('')
        }}>
          <option value="fastrouter">FastRouter</option>
          {!isFastRouter(values.base_url) && <option value="custom" disabled>Existing custom configuration</option>}
        </select></label>
        <p><a href="https://fastrouter.ai/" target="_blank" rel="noreferrer">Get a FastRouter API key</a>. Sign in, open your project’s Keys section, and create a key for Watson. Set a spending limit and paste the key below.</p>
        {credentialField}
        <details><summary>Advanced settings</summary>{connectionFields}<p>Uses the Anthropic-compatible API. Keep your working endpoint and model unless you need to change them.</p></details>
      <div className="integration-actions">
        <button className="settings-primary" type="submit" disabled={actionBusy}>{busy === 'save' ? 'Saving…' : integration?.configured ? 'Save changes' : 'Save & connect'}</button>
        <button type="button" onClick={test} disabled={actionBusy || !integration?.configured}>{busy === 'test' ? 'Testing…' : 'Test connection'}</button>
        {(integration?.configured || integration?.oauth_connected) && <button className="settings-danger" type="button" onClick={() => setConfirmDisconnect(true)} disabled={actionBusy}>Disconnect</button>}
      </div>
    </form>}
    {message && <p className={message.includes('could not') || message.includes('needs attention') ? 'settings-error' : 'settings-success'} role="status">{message}</p>}
    {source === 'clickup' && integration?.oauth_connected && !integration?.reauth_required && <ClickUpDestination integration={integration} onChanged={onChanged} />}
    {source === 'gitlab' && integration?.configured && <GitLabProjectSettings />}
    {confirmDisconnect && <div className="settings-modal-backdrop" role="presentation"><div className="settings-modal" role="dialog" aria-modal="true" aria-labelledby={`${source}-disconnect-title`}><h4 id={`${source}-disconnect-title`}>Disconnect {title}?</h4><p>Local work and cached history remain in Watson. Future sync for this integration will stop until you reconnect it.</p><div><button className="settings-danger" type="button" onClick={disconnect} disabled={actionBusy}>{busy === 'disconnect' ? 'Disconnecting…' : 'Disconnect'}</button><button type="button" onClick={() => setConfirmDisconnect(false)} disabled={actionBusy}>Cancel</button></div></div></div>}
  </article>
}

export default function IntegrationSettings({ integrations = {}, onChanged, compact = false }) {
  return <section className={`settings-section integration-settings${compact ? ' settings-compact' : ''}`} aria-labelledby="integrations-settings-title">
    <div className="settings-section-heading"><div><p className="eyebrow">Integrations</p><h2 id="integrations-settings-title">Local connections</h2><p>Credentials are stored only in macOS Keychain. Watson never displays a stored secret.</p></div></div>
    <div className="integration-settings-list">{INTEGRATIONS.map((definition) => <IntegrationCard key={definition.source} definition={definition} integration={integrations[definition.source]} onChanged={onChanged} />)}</div>
  </section>
}

export { SOURCE_LABEL }
