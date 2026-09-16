"""Durable local suppression. All checks read retained links, never providers."""


def removed_at(conn, item_id):
    row = conn.execute(
        'SELECT removed_at FROM work_removals WHERE work_item_id=? AND restored_at IS NULL',
        (item_id,),
    ).fetchone()
    return row['removed_at'] if row else None


def source_removed(conn, source, external_id):
    return bool(conn.execute(
        'SELECT 1 FROM work_links wl WHERE wl.source_type=? AND wl.external_id=? AND ('
        'EXISTS(SELECT 1 FROM work_removals r WHERE r.work_item_id=wl.work_item_id AND r.restored_at IS NULL) OR '
        'EXISTS(SELECT 1 FROM work_link_removals r WHERE r.link_id=wl.id AND r.restored_at IS NULL))',
        (source, external_id),
    ).fetchone())


def mr_removed(conn, mr_id):
    if source_removed(conn, 'gitlab_mr', mr_id):
        return True
    # A new MR can identify a removed ClickUp source without already having a link.
    from .gitlab_client import clickup_id_from_mr
    row = conn.execute('SELECT * FROM gitlab_mrs_cache WHERE mr_id=?', (mr_id,)).fetchone()
    task_id = clickup_id_from_mr(row) if row else None
    return bool(task_id and source_removed(conn, 'clickup', task_id))


def group_removed(conn, group):
    return bool(
        (group.get('work_item_id') and removed_at(conn, group['work_item_id']))
        or any(mr_removed(conn, mr_id) for mr_id in group.get('mr_ids', ()))
        or any(source_removed(conn, 'clickup', group.get(key))
               for key in ('clickup_task_id', 'related_clickup_task_id') if group.get(key))
    )


def require_sources(conn, mr_ids=(), *, item_id=None, clickup_id=None):
    if group_removed(conn, {'work_item_id': item_id, 'mr_ids': mr_ids, 'clickup_task_id': clickup_id}):
        raise ValueError('Work or source was removed from Watson; restore it first.')


def require_not_creating(conn, item_id, link=None):
    """Called inside the tombstone transaction; claims check the same tombstones."""
    from . import review_groups
    links = [link] if link else conn.execute('SELECT * FROM work_links WHERE work_item_id=?', (item_id,)).fetchall()
    mrs = {row['external_id'] for row in links if row['source_type'] == 'gitlab_mr'}
    tasks = {row['external_id'] for row in links if row['source_type'] == 'clickup'}
    for row in conn.execute("SELECT id FROM review_groups WHERE creation_state='creating'").fetchall():
        group = review_groups._get_group(conn, row['id'])
        if group['work_item_id'] == item_id or mrs.intersection(group['mr_ids']) or tasks.intersection(
            (group.get('clickup_task_id'), group.get('related_clickup_task_id'))
        ):
            raise ValueError('Review task creation is already in progress. Wait for it to finish before removing work.')
