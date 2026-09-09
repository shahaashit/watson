import test from 'node:test'
import assert from 'node:assert/strict'
import { renderJsx } from './renderJsx.js'

test('review automation settings render enabled defaults and explain boundaries', async () => {
  const markup = await renderJsx('src/components/ReviewAutomationSettings.jsx', {
    values: { auto_create_review_tasks: true, ai_group_review_mrs: true },
  })
  assert.match(markup, /Create ClickUp review tasks automatically/)
  assert.match(markup, /Group related review MRs using AI/)
  assert.equal((markup.match(/checked=""/g) || []).length, 2)
  assert.match(markup, /Comments, status changes, and closures still require approval/)
  assert.match(markup, /Exact ClickUp and branch matching stays enabled/)
})

test('profile settings expose the configurable identity email domain', async () => {
  const markup = await renderJsx('src/components/ProfileSettings.jsx', {
    profile: {
      display_name: 'Taylor', timezone: 'UTC', email_domain: 'engineering.example',
      auto_create_review_tasks: true, ai_group_review_mrs: true,
    },
  })
  assert.match(markup, /Email domain/)
  assert.match(markup, /value="engineering\.example"/)
  assert.match(markup, /ClickUp, Calendar, and Flock identities/)
})

test('ambiguous review grouping offers Merge and Keep separate', async () => {
  const markup = await renderJsx('src/components/WatsonSuggestions.jsx', {
    items: [{
      id: 31, kind: 'group_review_mrs', status: 'pending',
      payload: { suggested_title: 'Keyword TTL', member_mr_ids: ['1!10', '2!20'], confidence: .78 },
    }],
  })
  assert.match(markup, /Watson suggests/)
  assert.match(markup, /Merge/)
  assert.match(markup, /Keep separate/)
  assert.match(markup, /1!10/)
  assert.match(markup, /2!20/)
})

test('failed and uncertain review creation cards expose only safe recovery choices', async () => {
  const markup = await renderJsx('src/components/WatsonSuggestions.jsx', {
    items: [
      {
        id: 32, group_id: 8, kind: 'clickup_create_task', status: 'failed', retryable: true,
        payload: { draft: { name: 'Review - Keyword TTL' }, member_mr_ids: ['1!10'] },
      },
      {
        id: 'review-group-9', group_id: 9, kind: 'review_group_reconciliation',
        status: 'uncertain', retryable: false,
        payload: { suggested_title: 'Related rollout', member_mr_ids: ['2!20'] },
      },
    ],
  })
  assert.match(markup, /Retry/)
  assert.match(markup, /Check ClickUp before resolving/)
  assert.equal((markup.match(/>Retry</g) || []).length, 1)
})

test('canonical uncertainty suppresses retry even for stale retryable input', async () => {
  const markup = await renderJsx('src/components/WatsonSuggestions.jsx', {
    items: [{
      id: 'review-group-8', group_id: 8, kind: 'review_group_reconciliation',
      status: 'uncertain', retryable: true,
      payload: { suggested_title: 'Keyword TTL', member_mr_ids: ['1!10'] },
    }],
  })

  assert.match(markup, /Check ClickUp before resolving/)
  assert.doesNotMatch(markup, />Retry</)
})

test('complete review suggestion queue keeps duplicate cleanup approval gated', async () => {
  const markup = await renderJsx('src/components/WatsonSuggestions.jsx', {
    items: [
      {
        id: 41, kind: 'group_review_mrs', status: 'pending',
        payload: { suggested_title: 'Ambiguous rollout', member_mr_ids: ['1!10', '2!20'] },
      },
      {
        id: 42, group_id: 8, kind: 'clickup_create_task', status: 'failed', retryable: true,
        payload: { draft: { name: 'Review - Definite failure' }, member_mr_ids: ['3!30'] },
      },
      {
        id: 'review-group-9', group_id: 9, kind: 'review_group_reconciliation',
        status: 'uncertain', retryable: false,
        payload: { suggested_title: 'Uncertain result', member_mr_ids: ['4!40'] },
      },
      {
        id: 43, kind: 'clickup_close_task', status: 'pending', target_id: 'CU-DUPLICATE',
        payload: { reason: 'Duplicate after merge approval' },
      },
    ],
  })

  assert.equal((markup.match(/watson-suggestion-card/g) || []).length, 3)
  assert.match(markup, /Merge/)
  assert.match(markup, /Keep separate/)
  assert.equal((markup.match(/>Retry</g) || []).length, 1)
  assert.match(markup, /Check ClickUp before resolving/)
  assert.match(markup, /Close task/)
  assert.match(markup, />Approve</)
  assert.match(markup, />Reject</)
})
