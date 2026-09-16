"""Additively migrate legacy managed task groups into local work items."""
import json
from contextlib import contextmanager

from ..config import settings
from . import events, work_items, work_suppression


@contextmanager
def _atomic(conn):
    """Make the whole additive backfill all-or-nothing for unexpected failures."""
    if conn.in_transaction:
        conn.execute("SAVEPOINT work_backfill")
        try:
            yield
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT work_backfill")
            conn.execute("RELEASE SAVEPOINT work_backfill")
            raise
        else:
            conn.execute("RELEASE SAVEPOINT work_backfill")
        return

    conn.execute("BEGIN")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def backfill_managed_work(conn) -> dict[str, int]:
    """Create local migration items and links without changing any legacy source row."""
    created = 0
    with _atomic(conn):
        self_person_id = work_items.ensure_self_person(conn, settings.watson_user_name)
        rows = conn.execute("SELECT * FROM managed_tasks ORDER BY id").fetchall()
        groups = {}
        for row in rows:
            mr_ids = [row['related_mr_id']]
            try:
                extra = json.loads(row['additional_mr_ids'] or '[]')
                if isinstance(extra, list):
                    mr_ids.extend(m for m in extra if isinstance(m, str))
            except (ValueError, TypeError):
                pass
            if any(work_suppression.mr_removed(conn, m) for m in mr_ids if m) or any(
                work_suppression.source_removed(conn, 'clickup', task_id)
                for task_id in (row['clickup_task_id'], row['related_clickup_task_id']) if task_id
            ):
                continue
            key = row["related_clickup_task_id"] or row["clickup_task_id"]
            groups.setdefault(key, []).append(row)

        for clickup_id, group in groups.items():
            existing = conn.execute(
                "SELECT wi.id FROM work_items wi JOIN work_links wl ON wl.work_item_id=wi.id "
                "WHERE wl.source_type='clickup' AND wl.external_id=?",
                (clickup_id,),
            ).fetchone()
            if existing:
                work_item_id = existing["id"]
            else:
                cached = conn.execute(
                    "SELECT name, url FROM clickup_tasks_cache WHERE task_id=?", (clickup_id,)
                ).fetchone()
                item = work_items.create_work_item(
                    conn,
                    title=(cached["name"] or f"ClickUp {clickup_id}") if cached else f"ClickUp {clickup_id}",
                    state=("done" if all(row["status"] == "closed" for row in group) else "next"),
                    owner_person_id=self_person_id,
                    origin="migration",
                )
                work_item_id = item["id"]
                work_items.add_work_link(
                    conn,
                    work_item_id,
                    source_type="clickup",
                    external_id=clickup_id,
                    url=(cached["url"] or "") if cached else "",
                )
                created += 1

            for row in group:
                mr_ids = [row["related_mr_id"]]
                try:
                    additional = json.loads(row["additional_mr_ids"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    additional = []
                if isinstance(additional, list):
                    mr_ids.extend(additional)
                for mr_id in dict.fromkeys(
                    mr_id for mr_id in mr_ids if isinstance(mr_id, str) and mr_id.strip()
                ):
                    try:
                        work_items.add_work_link(
                            conn,
                            work_item_id,
                            source_type="gitlab_mr",
                            external_id=mr_id,
                        )
                    except ValueError as error:
                        if str(error) != "external link is already linked to another work item":
                            raise
                        already_reported = conn.execute(
                            "SELECT 1 FROM system_events WHERE kind='backfill_conflict' "
                            "AND related_mr_id=? AND related_task_id=?",
                            (mr_id, clickup_id),
                        ).fetchone()
                        if already_reported is None:
                            events.record(
                                conn,
                                "backfill_conflict",
                                f"Skipped MR {mr_id}: already linked to another work item",
                                details={"work_item_id": work_item_id, "clickup_id": clickup_id},
                                mr_id=mr_id,
                                task_id=clickup_id,
                            )
        if created:
            events.record(conn, "backfill", f"Migrated {created} Watson work items")
    return {"created": created}
