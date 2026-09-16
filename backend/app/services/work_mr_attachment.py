"""Attach one configured-host MR, with provider reads outside local mutations."""
import json
import re
from urllib.parse import urlsplit, urlunsplit

from ..models import now_iso
from . import events, gitlab_client, work_items, work_suppression


class InvalidMRURL(ValueError):
    pass


def _url(value, config):
    try:
        candidate = urlsplit(value.strip())
        base = urlsplit(config['base_url'].rstrip('/').removesuffix('/api/v4'))
        def origin(parsed):
            return (parsed.scheme.lower(), parsed.hostname,
                    parsed.port or (443 if parsed.scheme == 'https' else 80))
        if (base.scheme not in ('http', 'https') or not base.hostname
                or origin(candidate) != origin(base) or candidate.username is not None
                or candidate.password is not None or any(ord(c) < 32 for c in value)):
            raise ValueError
        path = candidate.path.rstrip('/')
        prefix = base.path.rstrip('/') + '/'
        if not path.startswith(prefix):
            raise ValueError
        match = re.fullmatch(r'([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)/-/merge_requests/([1-9][0-9]*)', path[len(prefix):])
        if not match or any(part in ('.', '..') for part in match[1].split('/')):
            raise ValueError
        return urlunsplit((base.scheme, base.netloc, path, '', '')), match[1], match[2]
    except (ValueError, KeyError, TypeError):
        raise InvalidMRURL('Enter a merge request URL from the configured GitLab server.') from None


def attach(conn, item_id, url):
    work_suppression.require_sources(conn, item_id=item_id)
    config = gitlab_client.gitlab_config()
    canonical, path, iid = _url(url, config)
    # Retained identities remain authoritative even after cache eviction.
    known = None
    for link in conn.execute("SELECT * FROM work_links WHERE source_type='gitlab_mr'"):
        try:
            matches = _url(link['url'], config)[0] == canonical
        except InvalidMRURL:
            matches = False
        if matches:
            work_suppression.require_sources(conn, [link['external_id']])
            if link['work_item_id'] != item_id:
                raise ValueError('external link is already linked to another work item')
            known = dict(link)
            break
    cached = None
    for row in conn.execute('SELECT * FROM gitlab_mrs_cache'):
        try:
            matches = _url(row['url'], config)[0] == canonical
        except InvalidMRURL:
            matches = False
        if matches:
            cached = dict(row)
            break
    fetched = None
    if cached is None and known is None:
        if not gitlab_client.configured(config):
            raise RuntimeError('GitLab credentials are required to fetch this merge request.')
        fetched = gitlab_client.fetch_mr_by_path(path, iid, config, explicit_attachment=True)
        if not fetched:
            raise RuntimeError('GitLab merge request could not be fetched.')
        fetched['url'] = canonical
    source = cached or fetched
    mr_id = source['mr_id'] if source else known['external_id']
    label = source['title'] if source else known['label']
    with work_items._mutation(conn):
        work_suppression.require_sources(conn, [mr_id], item_id=item_id)
        if fetched:
            # Write only after network completion, and never overwrite a cache
            # entry supplied by a concurrent sync with richer role evidence.
            data = dict(fetched)
            for key in ('roles', 'assignee_usernames', 'reviewer_usernames', 'labels'):
                data[key] = json.dumps(data.get(key) or [])
            data.update(stages='[]', synced_at=now_iso())
            columns = ('mr_id', 'project', 'title', 'state', 'url', 'role', 'roles',
                       'author', 'author_username', 'assignee_usernames', 'reviewer_usernames',
                       'source_branch', 'description', 'updated_at', 'labels', 'stages', 'synced_at')
            conn.execute(f"INSERT OR IGNORE INTO gitlab_mrs_cache ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                         tuple(data[key] for key in columns))
        existing = conn.execute("SELECT id FROM work_links WHERE source_type='gitlab_mr' AND external_id=?", (mr_id,)).fetchone()
        link = work_items.add_work_link(conn, item_id, source_type='gitlab_mr', external_id=mr_id, url=canonical, label=label)
        conn.execute("UPDATE work_items SET origin='manual' WHERE id=?", (item_id,))
        if existing is None:
            work_items.add_activity(conn, item_id, activity_type='system', body='Merge request attached.',
                                    metadata={'kind': 'work_mr_attached', 'link_id': link['id']})
            events.record(conn, 'work_mr_attached', 'Merge request attached.',
                          details={'work_item_id': item_id, 'link_id': link['id']}, mr_id=mr_id)
    return work_items.get_work_detail(conn, item_id)
