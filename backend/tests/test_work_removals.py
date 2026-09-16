"""Reversible suppression uses scratch SQLite and no provider operations."""
import pytest

from app.services import work_items, work_ingestion, work_backfill, review_automation, review_groups, gitlab_client, mr_review_tracker
from .test_review_automation import seed_mr


def make_work(client, conn, *, state='next', origin='manual'):
    item = client.post('/api/work-items', json={'title': 'Synthetic retained work', 'state': state}).json()['work_item']
    conn.execute('UPDATE work_items SET origin=? WHERE id=?', (origin, item['id']))
    conn.commit()
    link = work_items.add_work_link(conn, item['id'], source_type='gitlab_mr', external_id='41!71', url='https://gitlab.example.com/demo/-/merge_requests/71', label='Synthetic MR')
    seed_mr(conn, '41!71')
    return item, link


def post(client, path):
    response = client.post(path)
    assert response.status_code == 200, response.text
    return response.json()['work_item']


@pytest.mark.parametrize('state', ['next', 'done'])
def test_remove_restore_preserves_lifecycle_history_and_is_idempotent(client, conn, state):
    item, link = make_work(client, conn, state=state)
    work_items.add_activity(conn, item['id'], activity_type='note', body='Keep this note')
    original = work_items.get_work_detail(conn, item['id'])
    path = f"/api/work-items/{item['id']}"
    removed = post(client, path + '/remove')
    assert removed['removed_at']
    assert post(client, path + '/remove') == removed
    assert client.get('/api/work-items/removed').json()['work_items'] == [{'id': item['id'], 'title': item['title'], 'removed_at': removed['removed_at']}]
    assert not client.get('/api/work-items/my').json()['items']
    assert all(not lane['items'] for lane in client.get('/api/work-items/team?me_mode=true').json()['lanes'])
    assert not any(r['kind'] in ('work_item', 'work_activity') for r in client.get('/api/search?q=retained').json()['results'])
    restored = post(client, path + '/restore')
    assert restored['removed_at'] is None
    assert restored['state'] == state
    assert restored['completed_at'] == original['completed_at']
    assert restored['links'] == original['links']
    assert any(a['body'] == 'Keep this note' for a in restored['activity'])
    assert post(client, path + '/restore') == restored
    assert conn.execute("SELECT count(*) FROM system_events WHERE kind IN ('work_removed','work_restored')").fetchone()[0] == 2
    assert len(restored['activity']) == len(original['activity']) + 2


def test_unlink_restore_keeps_rows_and_blocks_ingestion_backfill_and_grouping(client, conn):
    item, link = make_work(client, conn, origin='discovery')
    path = f"/api/work-items/{item['id']}/links/{link['id']}"
    removed = post(client, path + '/remove')
    assert removed['links'] == []
    assert removed['removed_links'][0]['id'] == link['id']
    assert post(client, path + '/remove') == removed
    conn.execute("INSERT INTO managed_tasks(clickup_task_id, related_mr_id, category) VALUES ('synthetic123','41!71','Review')")
    conn.commit()
    for _ in range(2):
        work_ingestion.ingest_cached_mrs(conn)
        work_backfill.backfill_managed_work(conn)
    assert work_items.get_work_detail(conn, item['id'])['links'] == []
    assert conn.execute('SELECT count(*) FROM work_items').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM work_links').fetchone()[0] == 1
    assert review_automation.collect_candidates(conn) == []
    assert gitlab_client._referenced_mr_ids(conn, {'41'}) == set()
    assert gitlab_client._active_discovered_mr_ids(conn, {'41'}) == set()
    assert mr_review_tracker.propose_review_tasks(conn) == 0
    with pytest.raises(ValueError, match='removed'):
        work_items.add_work_link(conn, item['id'], source_type='gitlab_mr', external_id='41!71')
    with pytest.raises(ValueError, match='removed'):
        review_groups.upsert_group(conn, fingerprint='singleton:41!71', author_username='alex', title='Synthetic', description='', provenance='singleton', confidence=1, mr_ids=['41!71'])
    restored = post(client, path + '/restore')
    assert restored['links'][0]['id'] == link['id']
    assert restored['removed_links'] == []
    assert post(client, path + '/restore') == restored


def test_removed_work_suppresses_pending_creation_and_stale_ai_plan(client, conn):
    item, link = make_work(client, conn, origin='discovery')
    plan = review_automation.build_plan(conn, ai_enabled=False, ai_ready=False)
    review_groups.reconcile_plan(conn, plan.groups, ())
    post(client, f"/api/work-items/{item['id']}/remove")
    def forbidden(*args, **kwargs):
        pytest.fail('removed work crossed provider boundary')
    result = review_automation.run(conn, profile={'auto_create_review_tasks': True, 'ai_group_review_mrs': True}, clickup_ready=True, ai_ready=True, semantic_fn=forbidden, create_fn=forbidden)
    assert result['created'] == 0
    review_groups.reconcile_plan(conn, plan.groups, ())
    work_ingestion.ingest_cached_mrs(conn)
    conn.execute("UPDATE gitlab_mrs_cache SET state='merged'")
    conn.commit()
    work_ingestion.retire_terminal_mr_work(conn)
    restored = post(client, f"/api/work-items/{item['id']}/restore")
    assert restored['origin'] == 'discovery'
    assert restored['state'] == 'next'
    assert conn.execute('SELECT count(*) FROM work_items').fetchone()[0] == 1


def test_remove_validation_and_overlapping_suppression(client, conn):
    item, link = make_work(client, conn)
    cu = work_items.add_work_link(conn, item['id'], source_type='clickup', external_id='synthetic123')
    assert client.post(f"/api/work-items/{item['id']}/links/{cu['id']}/remove").status_code == 409
    assert client.post('/api/work-items/999/remove').status_code == 404
    assert client.post(f"/api/work-items/{item['id']}/links/999/remove").status_code == 404
    post(client, f"/api/work-items/{item['id']}/links/{link['id']}/remove")
    post(client, f"/api/work-items/{item['id']}/remove")
    post(client, f"/api/work-items/{item['id']}/restore")
    assert len(work_items.get_work_detail(conn, item['id'])['removed_links']) == 1
    assert review_automation.collect_candidates(conn) == []


@pytest.mark.parametrize('unlink', [False, True])
def test_creating_group_rejects_removal_without_tombstone_or_audit(client, conn, unlink):
    item, link = make_work(client, conn)
    group = review_groups.upsert_group(conn, fingerprint='singleton:41!71', author_username='alex', title='Synthetic', description='', provenance='singleton', confidence=1, mr_ids=['41!71'])
    assert review_groups.claim_creation(conn, group['id'])
    path = f"/api/work-items/{item['id']}" + (f"/links/{link['id']}" if unlink else '') + '/remove'
    response = client.post(path)
    assert response.status_code == 409
    assert 'already in progress' in response.json()['detail']
    assert conn.execute('SELECT count(*) FROM work_removals').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM work_link_removals').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM work_activity').fetchone()[0] == 0


def test_removed_group_cannot_be_claimed_or_retried_and_survivor_cannot_merge(client, conn):
    item, link = make_work(client, conn)
    group = review_groups.upsert_group(conn, fingerprint='singleton:41!71', author_username='alex', title='Synthetic', description='', provenance='singleton', confidence=1, mr_ids=['41!71'])
    review_groups.mark_deferred(conn, group['id'])
    post(client, f"/api/work-items/{item['id']}/remove")
    assert not review_groups.claim_creation(conn, group['id'], allow_failed=True)
    with pytest.raises(ValueError):
        review_automation.retry_group_creation(conn, group['id'], create_fn=lambda *args: pytest.fail('provider write'))
    other = work_items.create_work_item(conn, title='Unrelated retained work')
    with pytest.raises(ValueError, match='removed'):
        work_items.merge_items(conn, item['id'], [other['id']])
    assert conn.execute('SELECT origin FROM work_items WHERE id=?', (other['id'],)).fetchone()[0] == 'manual'


def test_removed_source_lookup_and_legacy_approval_are_blocked(client, conn, monkeypatch):
    from app.services import link_proposer
    item, link = make_work(client, conn)
    post(client, f"/api/work-items/{item['id']}/links/{link['id']}/remove")
    conn.execute('DELETE FROM gitlab_mrs_cache')
    conn.execute("INSERT INTO managed_tasks(clickup_task_id, category) VALUES ('synthetic123','Review')")
    managed_id = conn.execute('SELECT id FROM managed_tasks').fetchone()[0]
    conn.commit()
    monkeypatch.setattr(gitlab_client, 'gitlab_config', lambda: {'base_url': 'https://gitlab.example.com', 'token': 'synthetic', 'username': 'alex'})
    monkeypatch.setattr(gitlab_client, 'fetch_mr_by_path', lambda *args: pytest.fail('suppressed MR looked up'))
    assert gitlab_client.ensure_mrs_cached(conn, link['url']) == []
    with pytest.raises(ValueError, match='removed'):
        link_proposer.apply_link_approval(conn, {'managed_task_id': managed_id, 'related_mr_id': '41!71'})


def test_new_mr_cannot_attach_to_removed_clickup_source(client, conn):
    item, link = make_work(client, conn)
    work_items.add_work_link(conn, item['id'], source_type='clickup', external_id='86example')
    post(client, f"/api/work-items/{item['id']}/remove")
    seed_mr(conn, '41!72', branch='feature_clickup86example')
    other = work_items.create_work_item(conn, title='Other work')
    with pytest.raises(ValueError, match='removed'):
        work_items.add_work_link(conn, other['id'], source_type='gitlab_mr', external_id='41!72')
    assert work_ingestion.ingest_cached_mrs(conn)['created'] == 0


def test_unlinked_mr_does_not_supply_repo_badge_relevance_or_me_mode(client, conn):
    item, link = make_work(client, conn, origin='discovery')
    conn.execute("UPDATE gitlab_mrs_cache SET project='demo/retained-history', roles='[\"reviewer\"]' WHERE mr_id='41!71'")
    conn.commit()
    row = conn.execute('SELECT * FROM work_items WHERE id=?', (item['id'],)).fetchone()
    assert work_items._gitlab_repositories(conn, item['id']) == ['demo/retained-history']
    assert work_items._discovery_is_team_relevant(conn, row)
    assert work_items._work_item_involves_self(conn, row)


    path = f"/api/work-items/{item['id']}/links/{link['id']}"
    post(client, path + '/remove')
    assert work_items._gitlab_repositories(conn, item['id']) == []
    assert not work_items._discovery_is_team_relevant(conn, row)
    assert not work_items._work_item_involves_self(conn, row)
    lanes = client.get('/api/work-items/team').json()['lanes']
    shown = next(work for lane in lanes for work in lane['items'] if work['id'] == item['id'])
    assert shown['repositories'] == []
    assert conn.execute('SELECT count(*) FROM work_links WHERE id=?', (link['id'],)).fetchone()[0] == 1
    post(client, path + '/restore')
    assert work_items._gitlab_repositories(conn, item['id']) == ['demo/retained-history']
    assert work_items._discovery_is_team_relevant(conn, row)
    assert work_items._work_item_involves_self(conn, row)


@pytest.mark.parametrize('retirement', ['missing', 'unselected'])
def test_unlinked_last_mr_does_not_retire_parent_after_cache_eviction(client, conn, retirement):
    from app.services import app_settings
    item, link = make_work(client, conn, origin='discovery')
    path = f"/api/work-items/{item['id']}/links/{link['id']}"
    original = work_items.get_work_detail(conn, item['id'])
    post(client, path + '/remove')
    conn.execute("DELETE FROM gitlab_mrs_cache WHERE mr_id='41!71'")
    conn.commit()
    app_settings.set_value(conn, 'integration.gitlab.projects', [])
    retire = (work_ingestion.retire_missing_gitlab_work if retirement == 'missing'
              else work_ingestion.retire_unselected_gitlab_work)
    assert retire(conn) == 0
    restored = post(client, path + '/restore')
    assert restored['origin'] == original['origin'] == 'discovery'
    assert restored['state'] == original['state']
    assert restored['completed_at'] == original['completed_at']
    assert restored['links'][0]['id'] == link['id']


@pytest.mark.parametrize('retirement', ['missing', 'unselected'])
def test_removed_mr_does_not_mask_retirement_of_remaining_active_source(client, conn, retirement):
    from app.services import app_settings
    item, link = make_work(client, conn, origin='discovery')
    work_items.add_work_link(conn, item['id'], source_type='gitlab_mr', external_id='42!72')
    post(client, f"/api/work-items/{item['id']}/links/{link['id']}/remove")
    # Only the intentionally removed source is cached and selected.
    app_settings.set_value(conn, 'integration.gitlab.projects', [{'id': 41, 'path': 'demo/selected'}])
    retire = (work_ingestion.retire_missing_gitlab_work if retirement == 'missing'
              else work_ingestion.retire_unselected_gitlab_work)
    assert retire(conn) == 1
    assert work_items.get_work_detail(conn, item['id'])['origin'] == 'ignored'
