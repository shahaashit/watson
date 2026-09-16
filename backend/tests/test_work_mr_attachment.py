import json

import pytest

from app.services import gitlab_client, work_items
from .test_review_automation import seed_mr, seed_clickup_task

URL = 'https://gitlab.example.com/group/project/-/merge_requests/71'


@pytest.fixture(autouse=True)
def provider_boundary(monkeypatch):
    monkeypatch.setattr(gitlab_client, 'gitlab_config', lambda: {
        'base_url': 'https://gitlab.example.com', 'token': 'synthetic', 'username': 'alex',
    })
    monkeypatch.setattr(gitlab_client, '_get', lambda *a, **k: pytest.fail('unexpected provider read'))


def cached(conn):
    seed_mr(conn, '41!71')
    conn.execute('UPDATE gitlab_mrs_cache SET url=?', (URL,))
    conn.commit()


def attach(client, item, url=URL):
    return client.post(f"/api/work-items/{item['id']}/gitlab-mrs", json={'url': url})


def test_cached_attachment_is_exact_idempotent_and_kept_for_sync(client, conn):
    item = work_items.create_work_item(conn, title='Existing', origin='discovery')
    cached(conn)
    for url in (URL + '/?view=changes#note', URL):
        response = attach(client, item, url)
        assert response.status_code == 200, response.text
        detail = response.json()['work_item']
        assert len(detail['links']) == 1
        assert detail['links'][0]['external_id'] == '41!71'
        assert detail['links'][0]['url'] == URL
    assert conn.execute('SELECT count(*) FROM work_items').fetchone()[0] == 1
    assert '41!71' in gitlab_client._referenced_mr_ids(conn, set())
    assert len(detail['activity']) == 1


@pytest.mark.parametrize('url', [
    URL.replace('gitlab.example.com', 'evil.example.com'),
    URL.replace('https:', 'http:'),
    URL.replace('gitlab.example.com', 'gitlab.example.com:444'),
    URL.replace('gitlab.example.com', 'user@gitlab.example.com'),
    URL + '/extra', URL.replace('group/project', 'group/../project'),
    URL.replace('group/project', 'group/%2e%2e/project'),
])
def test_invalid_origin_or_path_never_fetches(client, conn, url):
    item = work_items.create_work_item(conn, title='Existing')
    assert attach(client, item, url).status_code == 422
    assert not work_items.get_work_detail(conn, item['id'])['links']


@pytest.mark.parametrize('mode', ['parent', 'source', 'other'])
def test_removed_or_conflicting_attachment_is_rejected(client, conn, mode):
    item = work_items.create_work_item(conn, title='Existing')
    if mode == 'parent':
        work_items.set_removed(conn, item['id'], True)
    else:
        cached(conn)
        other = work_items.create_work_item(conn, title='Other')
        link = work_items.add_work_link(conn, other['id'], source_type='gitlab_mr', external_id='41!71', url=URL)
        if mode == 'source':
            work_items.set_removed(conn, other['id'], True, link_id=link['id'])
            conn.execute('DELETE FROM gitlab_mrs_cache')
            conn.commit()
    assert attach(client, item).status_code == 409
    assert not work_items.get_work_detail(conn, item['id'])['links']


def test_uncached_attachment_reads_without_write_lock_or_invented_role(client, conn, monkeypatch):
    item = work_items.create_work_item(conn, title='Existing')
    class Response:
        def __init__(self, value): self.value = value
        def raise_for_status(self): pass
        def json(self): return self.value
    def read(url, **kwargs):
        conn.execute('BEGIN IMMEDIATE')
        conn.rollback()
        if url.endswith('/projects/group%2Fproject'):
            return Response({'id': 41})
        assert url.endswith('/projects/41/merge_requests/71')
        return Response({'project_id': 41, 'iid': 71, 'title': 'Exact MR',
                         'web_url': URL, 'state': 'opened', 'author': {'username': 'sam', 'name': 'Sam'},
                         'assignees': [], 'reviewers': [], 'labels': []})
    monkeypatch.setattr(gitlab_client, '_get', read)
    response = attach(client, item)
    assert response.status_code == 200, response.text
    assert response.json()['work_item']['links'][0]['label'] == 'Exact MR'
    row = conn.execute('SELECT * FROM gitlab_mrs_cache').fetchone()
    assert row['role'] != 'reviewer'
    assert 'reviewer' not in json.loads(row['roles'])
    assert conn.execute('SELECT count(*) FROM work_items').fetchone()[0] == 1


@pytest.mark.parametrize('kind', ['managed', 'group', 'prefix', 'original', 'unknown'])
def test_clickup_link_task_kind(client, conn, kind):
    item = work_items.create_work_item(conn, title='Existing')
    if kind != 'unknown':
        seed_clickup_task(conn, 'synthetic', 'Review - Synthetic' if kind == 'prefix' else 'Synthetic')
    if kind == 'managed':
        conn.execute("INSERT INTO managed_tasks(clickup_task_id,category) VALUES ('synthetic','Review')")
    if kind == 'group':
        conn.execute("INSERT INTO review_groups(fingerprint,title,clickup_task_id,provenance,confidence,created_at,updated_at) VALUES ('synthetic','Synthetic','synthetic','user',1,'now','now')")
    conn.commit()
    work_items.add_work_link(conn, item['id'], source_type='clickup', external_id='synthetic')
    detail = client.get(f"/api/work-items/{item['id']}").json()['work_item']
    assert detail['links'][0]['task_kind'] == ('original' if kind == 'original' else 'task' if kind == 'unknown' else 'review')


def test_cached_attachment_needs_no_token_and_preserves_done(client, conn, monkeypatch):
    monkeypatch.setattr(gitlab_client, 'gitlab_config', lambda: {'base_url': 'https://gitlab.example.com', 'token': '', 'username': ''})
    item = work_items.create_work_item(conn, title='Done', state='done')
    cached(conn)
    response = attach(client, item)
    assert response.status_code == 200
    detail = response.json()['work_item']
    assert detail['state'] == 'done'
    assert detail['completed_at'] == item['completed_at']


@pytest.mark.parametrize('evidence', ['none', 'reviewer', 'engaged', 'managed'])
def test_explicit_attachment_sync_does_not_invent_review_candidates(client, conn, monkeypatch, evidence):
    from app.services import review_automation
    from .test_gitlab_people_sync import _patch_successful_required_discovery, _mr
    item = work_items.create_work_item(conn, title='Existing')
    cached(conn)
    assert attach(client, item).status_code == 200
    if evidence == 'managed':
        conn.execute("INSERT INTO managed_tasks(clickup_task_id,category,related_mr_id) VALUES ('synthetic','Review','41!71')")
        conn.commit()
    _patch_successful_required_discovery(monkeypatch)
    monkeypatch.setattr(gitlab_client, 'fetch_engaged_mr_ids', lambda *a: {'41!71'} if evidence == 'engaged' else set())
    monkeypatch.setattr(gitlab_client, '_has_my_approval', lambda *a: False)
    def fetch(mr_id, role, config=None):
        assert mr_id == '41!71'
        return gitlab_client._normalize_mr(_mr(iid=71, assignees=(), reviewers=('alex',) if evidence == 'reviewer' else ()), role)
    monkeypatch.setattr(gitlab_client, '_fetch_mr_dict', fetch)
    assert gitlab_client.sync(conn) == 1
    row = conn.execute("SELECT role,roles FROM gitlab_mrs_cache WHERE mr_id='41!71'").fetchone()
    candidates = review_automation.collect_candidates(conn)
    if evidence == 'none':
        assert row['role'] not in ('reviewer', 'engaged')
        assert 'reviewer' not in json.loads(row['roles'])
        assert candidates == []
    elif evidence == 'managed':
        assert row['role'] == 'reviewer'
    else:
        assert [candidate.mr_id for candidate in candidates] == ['41!71']
