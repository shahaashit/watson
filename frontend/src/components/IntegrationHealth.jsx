import { useState } from 'react'
import useDismissibleLayer from '../useDismissibleLayer.js'

const HEALTH_LABELS = { healthy: 'Connected', degraded: 'Needs attention', disabled: 'Disabled', unconfigured: 'Not configured' }

function sourceLabel(source) {
  return { clickup: 'ClickUp', gitlab: 'GitLab', 'google-calendar': 'Google', 'review-automation': 'Review automation' }[source] || source
}

function reviewNeedsAttention(source) {
  if (source.source !== 'review-automation') return true
  const counts = source.counts || {}
  return ['deferred', 'failed', 'uncertain'].some((key) => Number(counts[key] || 0) > 0)
}

function reviewSummary(source) {
  if (source.source !== 'review-automation') return ''
  const counts = source.counts || {}
  return `Deferred ${counts.deferred || 0} · Failed ${counts.failed || 0} · Uncertain ${counts.uncertain || 0}`
}

function formatAge(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return ''
  if (seconds < 60) return 'Cached just now'
  if (seconds < 3600) return `Cached ${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `Cached ${Math.floor(seconds / 3600)}h ago`
  return `Cached ${Math.floor(seconds / 86400)}d ago`
}

function formatRetry(retryAt) {
  if (!retryAt) return ''
  const date = new Date(retryAt)
  return Number.isNaN(date.getTime()) ? 'Retry is scheduled' : `Retry scheduled ${date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}`
}

export default function IntegrationHealth({ sources = [], loading, error, onOpenSettings, onRetry }) {
  const [expanded, setExpanded] = useState(false)
  const [retrying, setRetrying] = useState('')
  const [retryError, setRetryError] = useState('')
  const rootRef = useDismissibleLayer({ open: expanded, onDismiss: () => setExpanded(false) })
  const visibleSources = sources.filter(source => source.source !== 'flock').filter(reviewNeedsAttention)
  const degraded = visibleSources.filter((source) => !['healthy', 'disabled'].includes(source.status)).length
  const label = loading ? 'Checking integrations' : degraded ? `${degraded} integration${degraded === 1 ? '' : 's'} need attention` : 'Integrations cached'
  const retry = async (source) => {
    if (!onRetry || retrying) return
    setRetrying(source.source); setRetryError('')
    try { await onRetry(source.source) }
    catch { setRetryError('Could not start a retry. Open Settings to review this integration.') }
    finally { setRetrying('') }
  }

  return (
    <div className="integration-health" ref={rootRef}>
      <button type="button" className={`integration-health-toggle${degraded ? ' degraded' : ''}`} onClick={() => setExpanded((open) => !open)} aria-expanded={expanded}>
        <span className="health-dot" aria-hidden="true" /> {label} <span aria-hidden="true">{expanded ? '⌃' : '⌄'}</span>
      </button>
      {expanded && (
        <div className="integration-health-details">
          {error ? <p>Health details are unavailable. Cached work remains available.</p> : visibleSources.length === 0 ? <p>No cached integration status yet.</p> : visibleSources.map((source) => (
            <div className="integration-health-row" key={source.source}>
              <span>{sourceLabel(source.source)}</span>
              <span>{HEALTH_LABELS[source.status] || 'Needs attention'}</span>
              {source.last_success_at && <time dateTime={source.last_success_at}>Updated {new Date(source.last_success_at).toLocaleDateString()}</time>}
              {source.message && <p className="integration-health-message">{source.message}</p>}
              {reviewSummary(source) && <span className="integration-health-meta">{reviewSummary(source)}</span>}
              {formatAge(source.cached_age_seconds) && <span className="integration-health-meta">{formatAge(source.cached_age_seconds)}</span>}
              {formatRetry(source.retry_at) && <span className="integration-health-meta">{formatRetry(source.retry_at)}</span>}
              {source.source !== 'review-automation' && source.status === 'degraded' && onRetry && <button type="button" className="health-retry" disabled={Boolean(retrying)} onClick={() => retry(source)}>{retrying === source.source ? 'Retrying…' : 'Retry now'}</button>}
            </div>
          ))}
          {retryError && <p className="integration-health-message" role="alert">{retryError}</p>}
          <button type="button" className="health-settings-link" onClick={onOpenSettings}>Open Settings</button>
        </div>
      )}
    </div>
  )
}
