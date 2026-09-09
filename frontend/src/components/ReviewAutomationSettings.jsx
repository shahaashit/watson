export default function ReviewAutomationSettings({ values, onChange, disabled = false }) {
  const set = (key) => (event) => onChange?.({
    ...values,
    [key]: event.target.checked,
  })

  return <fieldset className="review-automation-settings" disabled={disabled}>
    <legend>Review automation</legend>
    <label>
      <input type="checkbox" checked={values.auto_create_review_tasks !== false}
        onChange={set('auto_create_review_tasks')} />
      <span><strong>Create ClickUp review tasks automatically</strong>
        <small>Comments, status changes, and closures still require approval.</small></span>
    </label>
    <label>
      <input type="checkbox" checked={values.ai_group_review_mrs !== false}
        onChange={set('ai_group_review_mrs')} />
      <span><strong>Group related review MRs using AI</strong>
        <small>Exact ClickUp and branch matching stays enabled when this is off.</small></span>
    </label>
  </fieldset>
}
