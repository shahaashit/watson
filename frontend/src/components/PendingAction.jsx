import { useState } from 'react'
import { api } from '../api.js'

const KIND_LABELS = {
  clickup_comment: 'Comment',
  clickup_status: 'Status change',
  clickup_create_task: 'New task',
  clickup_close_task: 'Close task',
  link_mr_to_task: 'Link MR',
  merge_managed_tasks: 'Merge duplicate cards',
  close_orphan_card: 'Close orphan card',
  group_review_mrs: 'Group review MRs',
}

export default function PendingAction({ action, onChanged }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(
    typeof action.payload.draft === 'object'
      ? JSON.stringify(action.payload.draft, null, 2)
      : String(action.payload.draft ?? '')
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const run = async (fn) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      onChanged?.()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const approve = () => run(async () => {
    if (editing) await saveDraft()
    await api.approveAction(action.id)
  })
  const reject = () => run(() => api.rejectAction(action.id))
  const saveDraft = async () => {
    let value = draft
    if (action.kind === 'clickup_create_task') {
      try { value = JSON.parse(draft) } catch { /* keep as string name */ }
    }
    await api.editAction(action.id, { payload: { draft: value } })
    setEditing(false)
  }

  const confidence = action.match_confidence
  // only comment/status actions need a confident match to an existing task
  const needsMatch = action.kind === 'clickup_comment' || action.kind === 'clickup_status'
  const lowConfidence = needsMatch && (!action.target_id || (confidence ?? 0) < 0.6)
  const isCreate = action.kind === 'clickup_create_task'
  const isLink = action.kind === 'link_mr_to_task'
  const isMerge = action.kind === 'merge_managed_tasks'
  const isOrphan = action.kind === 'close_orphan_card'
  const taskObj = (() => {
    if (!isCreate) return null
    try { return typeof action.payload.draft === 'object' ? action.payload.draft : JSON.parse(draft) }
    catch { return null }
  })()

  if (action.status !== 'pending') {
    return (
      <div className={`pending-action resolved ${action.status}`}>
        <span className="kind">{KIND_LABELS[action.kind]}</span>
        <span className="action-status">{action.status}</span>
        {action.error && <span className="error-text">{action.error}</span>}
      </div>
    )
  }

  if (isOrphan) {
    const p = action.payload || {}
    return (
      <div className="pending-action link-action">
        <div className="action-head">
          <span className="kind">{KIND_LABELS[action.kind]}</span>
        </div>
        <div className="link-body">
          <div className="link-row">
            <span className="link-label">Card</span>
            <span className="link-value">📋 #{p.managed_task_id} → ClickUp {p.clickup_task_id}</span>
          </div>
          <div className="link-row">
            <span className="link-label">Reason</span>
            <span className="link-value">{p.reason || 'ClickUp task no longer exists'}</span>
          </div>
        </div>
        <p className="link-help">
          The ClickUp task this card was tracking appears to have been deleted.
          Approving will mark this Watson card closed locally — nothing is
          written to ClickUp (it's already gone).
        </p>
        <div className="action-buttons">
          <button className="btn approve" disabled={busy} onClick={approve}>
            Close card
          </button>
          <button className="btn reject" disabled={busy} onClick={reject}>Reject</button>
        </div>
        {error && <div className="error-text">{error}</div>}
      </div>
    )
  }

  if (isMerge) {
    const p = action.payload || {}
    return (
      <div className="pending-action link-action">
        <div className="action-head">
          <span className="kind">{KIND_LABELS[action.kind]}</span>
        </div>
        <div className="link-body">
          <div className="link-row">
            <span className="link-label">Keep</span>
            <span className="link-value">📋 {p.survivor_name}</span>
          </div>
          <div className="link-row">
            <span className="link-label">Merge in</span>
            <span className="link-value">📋 {p.duplicate_name}</span>
          </div>
          {p.duplicate_mr_id && (
            <div className="link-row">
              <span className="link-label">MR</span>
              <span className="link-value">🔀 {p.duplicate_mr_id}</span>
            </div>
          )}
        </div>
        <p className="link-help">
          These two cards point at the same parent ClickUp task. Approving
          will attach the duplicate's MR to the surviving card, hide the
          duplicate from Work, and queue a separate close-task action you
          can approve for ClickUp.
        </p>
        <div className="action-buttons">
          <button className="btn approve" disabled={busy} onClick={approve}>
            Approve merge
          </button>
          <button className="btn reject" disabled={busy} onClick={reject}>Reject</button>
        </div>
        {error && <div className="error-text">{error}</div>}
      </div>
    )
  }

  if (isLink) {
    const p = action.payload || {}
    const scorePct = typeof p.match_score === 'number' ? Math.round(p.match_score) : null
    return (
      <div className="pending-action link-action">
        <div className="action-head">
          <span className="kind">{KIND_LABELS[action.kind]}</span>
          {scorePct != null && <span className="target"><em>{scorePct}% match</em></span>}
        </div>
        <div className="link-body">
          <div className="link-row">
            <span className="link-label">MR</span>
            <span className="link-value">🔀 {p.mr_title}</span>
          </div>
          <div className="link-row">
            <span className="link-label">Task</span>
            <span className="link-value">📋 {p.task_name}</span>
          </div>
        </div>
        <p className="link-help">
          Watson thinks this MR is part of the same work as the task above.
          Approve to show it on the task's Work card.
        </p>
        <div className="action-buttons">
          <button className="btn approve" disabled={busy} onClick={approve}>
            Approve link
          </button>
          <button className="btn reject" disabled={busy} onClick={reject}>Reject</button>
        </div>
        {error && <div className="error-text">{error}</div>}
      </div>
    )
  }

  return (
    <div className="pending-action">
      <div className="action-head">
        <span className="kind">{KIND_LABELS[action.kind]}</span>
        {action.target_id && (
          <span className="target">
            → {action.target_id}
            {confidence != null && <em> ({Math.round(confidence * 100)}% match)</em>}
          </span>
        )}
        {isCreate && <span className="target">→ assigned to you</span>}
        {lowConfidence && <span className="warn">no confident task match</span>}
      </div>
      {editing ? (
        <textarea
          className="draft-edit"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={isCreate ? 7 : 3}
        />
      ) : isCreate && taskObj ? (
        <div className="task-draft">
          <div className="task-name">📋 {taskObj.name}</div>
          {Array.isArray(action.payload?.additional_mr_ids) && action.payload.additional_mr_ids.length > 0 && (
            <div className="grouped-mrs" title={action.payload.additional_mr_ids.join(', ')}>
              + {action.payload.additional_mr_ids.length} more MR
              {action.payload.additional_mr_ids.length === 1 ? '' : 's'} grouped
              {' '}({action.payload.additional_mr_ids.join(', ')})
            </div>
          )}
          {taskObj.description && <pre className="draft">{taskObj.description}</pre>}
          {Array.isArray(taskObj.labels) && taskObj.labels.length > 0 && (
            <div className="task-labels">
              {taskObj.labels.map((l) => <span key={l} className="chip tag">#{l}</span>)}
            </div>
          )}
        </div>
      ) : (
        <pre className="draft">{draft}</pre>
      )}
      <div className="action-buttons">
        <button className="btn approve" disabled={busy || (lowConfidence && !editing)} onClick={approve}>
          Approve
        </button>
        {editing ? (
          <button className="btn" disabled={busy} onClick={() => run(saveDraft)}>Save</button>
        ) : (
          <button className="btn" disabled={busy} onClick={() => setEditing(true)}>Edit</button>
        )}
        <button className="btn reject" disabled={busy} onClick={reject}>Reject</button>
      </div>
      {lowConfidence && (
        <TargetPicker action={action} onChanged={onChanged} disabled={busy} />
      )}
      {error && <div className="error-text">{error}</div>}
    </div>
  )
}

function TargetPicker({ action, onChanged, disabled }) {
  const [taskId, setTaskId] = useState('')
  const save = async () => {
    if (!taskId.trim()) return
    await api.editAction(action.id, { target_id: taskId.trim() })
    onChanged?.()
  }
  return (
    <div className="target-picker">
      <input
        placeholder="ClickUp task id…"
        value={taskId}
        onChange={(e) => setTaskId(e.target.value)}
        disabled={disabled}
      />
      <button className="btn" onClick={save} disabled={disabled || !taskId.trim()}>
        Set task
      </button>
    </div>
  )
}
