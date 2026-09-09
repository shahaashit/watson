"""ClickUp REST connector. Reads sync the task cache; writes only run via
the pending_actions approval flow — never directly."""
import hashlib
import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import partial
from types import MappingProxyType

import requests

from ..config import settings
from ..models import now_iso
from . import app_settings, external_errors, secret_store
from .clickup_errors import workspace_denied

log = logging.getLogger("watson.clickup")

BASE = "https://api.clickup.com/api/v2"
TIMEOUT = 20
# ClickUp's public rate limit is 100 requests / minute / user. 10 concurrent
# workers on the referenced-task refresh keeps us comfortably below that even
# during the heaviest sync tick (main-list pagination + referenced fetches
# combined) while cutting ~70 sequential HTTPs from ~20s to ~2s wall-clock.
REFERENCED_FETCH_WORKERS = 10


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0


class CircuitOpenError(RuntimeError):
    """Raised locally when ClickUp is cooling down after repeated failures."""


@dataclass(frozen=True)
class CircuitPermit:
    credential_key: bytes
    epoch: int
    lease: int


class CircuitBreaker:
    """Process-local, credential-aware circuit breaker for exact task reads."""

    def __init__(self, failure_threshold: int = 3, cooldown_seconds: int = 120):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._lock = threading.Lock()
        self._credential_key = None
        self._failures = 0
        self._opened_at = None
        self._state = "closed"
        self._epoch = 0
        self._next_lease = 0
        self._active_leases = set()

    def _advance_epoch(self) -> None:
        self._epoch += 1
        self._active_leases.clear()

    def _issue_permit(self, credential_key: bytes) -> CircuitPermit:
        self._next_lease += 1
        permit = CircuitPermit(credential_key, self._epoch, self._next_lease)
        self._active_leases.add(permit.lease)
        return permit

    def _open(self) -> None:
        if self._state != "open":
            self._state = "open"
            self._opened_at = time.monotonic()
            self._advance_epoch()

    def _is_active(self, permit: CircuitPermit) -> bool:
        return (
            self._credential_key == permit.credential_key
            and self._epoch == permit.epoch
            and permit.lease in self._active_leases
        )

    def permit(self, credential_key: bytes) -> CircuitPermit | None:
        """Lease one request, or deny it while the circuit is unavailable."""
        with self._lock:
            if self._credential_key != credential_key:
                self._credential_key = credential_key
                self._failures = 0
                self._opened_at = None
                self._state = "closed"
                self._advance_epoch()
            if self._state == "open":
                if time.monotonic() - self._opened_at < self.cooldown_seconds:
                    return None
                self._state = "half_open"
                self._failures = 0
                self._advance_epoch()
                return self._issue_permit(credential_key)
            if self._state == "half_open":
                return None
            return self._issue_permit(credential_key)

    def record_success(self, permit: CircuitPermit) -> None:
        with self._lock:
            if not self._is_active(permit):
                return
            self._active_leases.remove(permit.lease)
            if self._state == "half_open":
                self._state = "closed"
                self._failures = 0
                self._opened_at = None
                self._advance_epoch()
            elif self._state == "closed":
                self._failures = 0

    def record_failure(self, permit: CircuitPermit) -> None:
        with self._lock:
            if not self._is_active(permit):
                return
            self._active_leases.remove(permit.lease)
            if self._state == "half_open":
                self._open()
            elif self._state == "closed":
                self._failures += 1
                if self._failures >= self.failure_threshold:
                    self._open()

    def release(self, permit: CircuitPermit) -> None:
        """Finalize a neutral permit outcome without counting a service failure."""
        with self._lock:
            if not self._is_active(permit):
                return
            self._active_leases.remove(permit.lease)
            if self._state == "half_open":
                self._state = "closed"
                self._failures = 0
                self._opened_at = None
                self._advance_epoch()


_TASK_CIRCUIT_BREAKER = CircuitBreaker()


def _request(method, *args, **kwargs):
    response = getattr(requests, method)(*args, **kwargs)
    authorization = kwargs.get('headers', {}).get('Authorization', '')
    if authorization.startswith('Bearer ') and response.status_code == 401 and not workspace_denied(response):
        from . import clickup_oauth
        clickup_oauth.mark_unauthorized(authorization)
    return response


def clickup_config() -> dict:
    """Resolve current ClickUp credentials and the UI-managed task list."""
    from . import gitlab_oauth
    try:
        # Reconnect clears the destination while replacing the credential.
        # Hold the same reentrant lock across every part of this snapshot.
        with gitlab_oauth.credential_lock():
            return _clickup_config_locked()
    except Exception:
        return MappingProxyType(
            {"token": "", "list_ids": (), "create_list_id": ""}
        )


def _clickup_config_locked() -> dict:
    if app_settings.integration_disabled("clickup"):
        return MappingProxyType(
            {"token": "", "list_ids": (), "create_list_id": ""}
        )
    try:
        stored_create_list_id = app_settings.effective_nonsecret(
            "integration.clickup.create_list_id", None
        )
        token = secret_store.effective_secret(
            "clickup.token", settings.clickup_api_token
        )
        from . import clickup_oauth
        oauth_token = clickup_oauth.access_token()
        if oauth_token is not None:
            token = oauth_token
    except Exception:
        return MappingProxyType(
            {"token": "", "list_ids": (), "create_list_id": ""}
        )

    if stored_create_list_id is None:
        create_list_id = settings.clickup_create_list_id.strip()
        list_ids = tuple(settings.clickup_list_id_list)
    else:
        create_list_id = str(stored_create_list_id or "").strip()
        list_ids = (create_list_id,) if create_list_id else ()
    if not create_list_id and list_ids:
        create_list_id = list_ids[0]
    return MappingProxyType({
        "token": token,
        "list_ids": list_ids,
        "create_list_id": create_list_id,
    })


def configured(config=None) -> bool:
    config = config or clickup_config()
    return bool(config["token"])


def _require_config(config=None, *, require_lists=False):
    config = config or clickup_config()
    if not configured(config) or (require_lists and not config["list_ids"]):
        raise RuntimeError("ClickUp is not configured")
    return config


def _headers(config=None) -> dict:
    config = _require_config(config)
    return {
        "Authorization": config["token"],
        "Content-Type": "application/json",
    }


def _ms_to_iso(ms):
    if not ms:
        return None
    return datetime.fromtimestamp(int(ms) / 1000).isoformat(timespec="seconds")


def _task_to_cache_row(t: dict) -> dict:
    """Shape one ClickUp API task into a clickup_tasks_cache row."""
    # collapse whitespace/newlines so each task name stays one clean line
    name = " ".join((t.get("name") or "").split())
    status = t.get("status") or {}
    return {
        "task_id": t["id"],
        "name": name,
        "status": status.get("status", ""),
        "status_type": status.get("type", ""),
        "list_name": (t.get("list") or {}).get("name", ""),
        "url": t.get("url", ""),
        "assignees": json.dumps(
            [a.get("username") or a.get("email", "") for a in t.get("assignees", [])]
        ),
        "due_date": _ms_to_iso(t.get("due_date")),
        "date_closed": _ms_to_iso(t.get("date_closed")),
    }


def get_task(task_id: str, config=None) -> dict:
    """Fetch a single task directly from ClickUp (live)."""
    config = _require_config(config)
    resp = _request("get",
        f"{BASE}/task/{task_id}", headers=_headers(config), timeout=TIMEOUT
    )
    resp.raise_for_status()
    return resp.json()


def _credential_key(config) -> bytes:
    return hashlib.sha256(config["token"].encode()).digest()


def _retry_after_seconds(response, wall_clock):
    if response is None:
        return None
    value = (response.headers or {}).get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, retry_at.timestamp() - wall_clock())
        except (TypeError, ValueError, IndexError, OverflowError):
            return None


def _retry_delay(
    response, attempt: int, policy: RetryPolicy, random_value, wall_clock
) -> float:
    retry_after = _retry_after_seconds(response, wall_clock)
    if retry_after is not None:
        return retry_after
    return min(policy.max_delay, policy.base_delay * (2 ** attempt)) + random_value()


def _retryable_response(response) -> bool:
    return response.status_code == 429 or response.status_code >= 500


def get_task_resilient(
    task_id: str,
    *,
    config=None,
    policy: RetryPolicy | None = None,
    sleeper=time.sleep,
    random_value=random.random,
    wall_clock=time.time,
) -> dict:
    """Fetch one exact task with bounded retries and a local circuit breaker."""
    # Keep every retry on the credential generation that began this operation;
    # a settings update must affect the next task read, never half of this one.
    resolved = clickup_config() if config is None else config
    config = _require_config(MappingProxyType(dict(resolved)))
    credential_key = _credential_key(config)
    permit = _TASK_CIRCUIT_BREAKER.permit(credential_key)
    if permit is None:
        raise CircuitOpenError("ClickUp exact-task requests are cooling down")

    policy = policy or RetryPolicy()
    attempts = max(1, int(policy.max_attempts))
    settled = False
    try:
        for attempt in range(attempts):
            response = None
            retry_wait = None
            try:
                response = _request("get",
                    f"{BASE}/task/{task_id}", headers=_headers(config), timeout=TIMEOUT
                )
                if _retryable_response(response):
                    if attempt == attempts - 1:
                        _TASK_CIRCUIT_BREAKER.record_failure(permit)
                        settled = True
                        response.raise_for_status()
                    retry_wait = _retry_delay(
                        response, attempt, policy, random_value, wall_clock
                    )
                else:
                    response.raise_for_status()
                    task = response.json()
            except (requests.Timeout, requests.ConnectionError):
                if attempt == attempts - 1:
                    _TASK_CIRCUIT_BREAKER.record_failure(permit)
                    settled = True
                    raise
                retry_wait = _retry_delay(
                    None, attempt, policy, random_value, wall_clock
                )
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass

            if retry_wait is not None:
                sleeper(retry_wait)
                continue

            _TASK_CIRCUIT_BREAKER.record_success(permit)
            settled = True
            return task
        raise RuntimeError("unreachable retry state")
    finally:
        if not settled:
            _TASK_CIRCUIT_BREAKER.release(permit)


def _upsert_task_row(conn, row: dict) -> None:
    conn.execute(
        "INSERT INTO clickup_tasks_cache (task_id, name, status, status_type, list_name, url, "
        "assignees, due_date, date_closed, synced_at) VALUES "
        "(:task_id, :name, :status, :status_type, :list_name, :url, :assignees, :due_date, "
        ":date_closed, :synced_at) ON CONFLICT(task_id) DO UPDATE SET "
        "name=excluded.name, status=excluded.status, status_type=excluded.status_type, "
        "list_name=excluded.list_name, url=excluded.url, assignees=excluded.assignees, "
        "due_date=excluded.due_date, date_closed=excluded.date_closed, synced_at=excluded.synced_at",
        {**row, "synced_at": now_iso()},
    )


def _stable_task_ids(task_ids):
    seen = set()
    for task_id in task_ids:
        task_id = str(task_id or "").strip()
        if task_id and task_id not in seen:
            seen.add(task_id)
            yield task_id


def refresh_exact_tasks(conn, task_ids, *, config=None) -> dict[str, int]:
    """Refresh exact linked task IDs without deleting a previously good cache."""
    resolved = clickup_config() if config is None else config
    config = MappingProxyType(dict(resolved))
    task_ids = list(_stable_task_ids(task_ids))
    result = {"updated": 0, "failed": 0, "skipped": 0}
    if not task_ids:
        return result
    if not configured(config):
        result["skipped"] = len(task_ids)
        return result

    with ThreadPoolExecutor(max_workers=4) as pool:
        worker = partial(_fetch_exact_task, config=config)
        outcomes = list(pool.map(worker, task_ids))

    rows = []
    for outcome in outcomes:
        if isinstance(outcome, CircuitOpenError):
            result["skipped"] += 1
            continue
        if isinstance(outcome, BaseException):
            result["failed"] += 1
            if workspace_denied(getattr(outcome, 'response', None)):
                result['restricted'] = result.get('restricted', 0) + 1
            external_errors.log_failure(log, "exact task refresh", "clickup", outcome)
            continue
        try:
            rows.append(_task_to_cache_row(outcome))
        except Exception as error:
            result["failed"] += 1
            external_errors.log_failure(log, "exact task cache shape", "clickup", error)

    if not rows:
        return result

    outer_transaction = conn.in_transaction
    try:
        if outer_transaction:
            conn.execute("SAVEPOINT clickup_exact_refresh")
        else:
            conn.execute("BEGIN")
        for row in rows:
            _upsert_task_row(conn, row)
        if outer_transaction:
            conn.execute("RELEASE SAVEPOINT clickup_exact_refresh")
        else:
            conn.commit()
    except Exception as error:
        if outer_transaction:
            try:
                conn.execute("ROLLBACK TO SAVEPOINT clickup_exact_refresh")
            finally:
                conn.execute("RELEASE SAVEPOINT clickup_exact_refresh")
        else:
            conn.rollback()
        result["failed"] += len(rows)
        external_errors.log_failure(log, "exact task cache update", "clickup", error)
        return result

    result["updated"] += len(rows)
    return result


def _fetch_exact_task(task_id: str, config=None):
    try:
        return get_task_resilient(task_id, config=config)
    except CircuitOpenError as error:
        return error
    except Exception as error:
        return error


def task_is_done(task: dict) -> bool:
    return ((task or {}).get("status") or {}).get("type") in ("closed", "done")


def refresh_tasks(conn, task_ids, *, commit: bool = True) -> int:
    """Force-fetch the given task ids and upsert them into the cache. Used after
    creating a task (so it appears immediately) and during sync to pick up tasks
    referenced by managed_tasks that aren't in the configured lists."""
    config = clickup_config()
    if not configured(config):
        return 0
    added = 0
    for tid in task_ids:
        if not tid:
            continue
        try:
            row = _task_to_cache_row(get_task(tid, config))
        except requests.RequestException as exc:
            external_errors.log_failure(
                log, f"refresh_tasks fetch {tid}", "clickup", exc
            )
            continue
        conn.execute(
            "INSERT OR REPLACE INTO clickup_tasks_cache"
            " (task_id, name, status, status_type, list_name, url,"
            "  assignees, due_date, date_closed, synced_at)"
            " VALUES (:task_id, :name, :status, :status_type, :list_name,"
            "         :url, :assignees, :due_date, :date_closed, :synced_at)",
            {**row, "synced_at": now_iso()},
        )
        added += 1
    if added and commit:
        conn.commit()
    return added


def get_cached_task(conn, task_id: str) -> dict:
    """A cached task in the same shape `get_task` returns (so callers can swap
    them transparently). Returns None on cache miss."""
    row = conn.execute(
        "SELECT * FROM clickup_tasks_cache WHERE task_id = ?", (task_id,)
    ).fetchone()
    if not row:
        return None
    return {
        "id": row["task_id"],
        "name": row["name"] or "",
        "url": row["url"] or "",
        "status": {"status": row["status"] or "", "type": row["status_type"] or ""},
        "assignees": [{"username": u} for u in json.loads(row["assignees"] or "[]")],
    }


def fetch_tasks(config=None) -> list:
    config = _require_config(config, require_lists=True)
    tasks = []
    for list_id in config["list_ids"]:
        page = 0
        while True:
            resp = _request("get",
                f"{BASE}/list/{list_id}/task",
                headers=_headers(config),
                params={"page": page, "include_closed": "false"},
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            batch = resp.json().get("tasks", [])
            for t in batch:
                tasks.append(_task_to_cache_row(t))
            if len(batch) < 100:
                break
            page += 1
    return tasks


def _fetch_referenced_task(tid: str, config=None):
    """Fetch one referenced task and shape it, or return None on failure.
    Used as the worker function in the parallel referenced-fetch pool."""
    try:
        return _task_to_cache_row(get_task(tid, config))
    except requests.RequestException as exc:
        external_errors.log_failure(
            log, f"referenced task refresh {tid}", "clickup", exc
        )
        return None


def sync(conn) -> int:
    """Replace the local cache with current open tasks from configured lists,
    PLUS any tasks referenced by managed_tasks (your tasks + related team tasks)
    so the Work view can read everything it needs from SQLite — no live API
    calls during page loads.

    Referenced-task fetches run in parallel (see REFERENCED_FETCH_WORKERS).
    The main list-page fetch stays serial — it's already fast enough (~2s for
    5 pages), and parallelizing it would require knowing the total count up
    front, which ClickUp's API doesn't hand out.
    """
    config = _require_config(require_lists=True)

    t0 = time.monotonic()
    tasks = fetch_tasks(config)
    t_main = time.monotonic() - t0
    fetched_ids = {t["task_id"] for t in tasks}

    # Referenced tasks: those pinned by managed_tasks (your tasks + related
    # team tasks) that aren't in the configured lists. We need them cached so
    # the Work view can render without live API calls.
    referenced = set()
    for r in conn.execute(
        "SELECT clickup_task_id, related_clickup_task_id FROM managed_tasks"
    ):
        if r["clickup_task_id"]:
            referenced.add(r["clickup_task_id"])
        if r["related_clickup_task_id"]:
            referenced.add(r["related_clickup_task_id"])

    to_fetch = list(referenced - fetched_ids)
    extras = 0
    t_ref = 0.0
    if to_fetch:
        t1 = time.monotonic()
        with ThreadPoolExecutor(max_workers=REFERENCED_FETCH_WORKERS) as pool:
            worker = partial(_fetch_referenced_task, config=config)
            for row in pool.map(worker, to_fetch):
                if row:
                    tasks.append(row)
                    extras += 1
        t_ref = time.monotonic() - t1

    # Dedupe by task_id before insert. Two ways a duplicate can land in `tasks`:
    #   (a) ClickUp's paginated list endpoint doesn't guarantee stable ordering,
    #       so a task can appear on two adjacent pages if its state shifts mid-fetch;
    #   (b) belt-and-suspenders against future duplicate sources.
    # Later entries win — the referenced-task fetch happens after main pagination,
    # so if a task ends up in both, the referenced-task copy is fresher.
    by_id = {t["task_id"]: t for t in tasks}
    dupes = len(tasks) - len(by_id)
    tasks = list(by_id.values())

    synced_at = now_iso()
    conn.execute("DELETE FROM clickup_tasks_cache")
    conn.executemany(
        "INSERT INTO clickup_tasks_cache (task_id, name, status, status_type,"
        " list_name, url, assignees, due_date, date_closed, synced_at) VALUES ("
        " :task_id, :name, :status, :status_type, :list_name, :url, :assignees,"
        " :due_date, :date_closed, :synced_at)",
        [{**t, "synced_at": synced_at} for t in tasks],
    )
    conn.commit()
    log.info(
        "clickup sync: %d tasks (incl. %d referenced, %d dupes dropped) — main %.1fs, referenced %.1fs (%dw)",
        len(tasks), extras, dupes, t_main, t_ref, REFERENCED_FETCH_WORKERS if to_fetch else 0,
    )
    return len(tasks)


_user_id_cache = None
_user_id_cache_key = None


def current_user_id(config=None):
    """The current ClickUp user's id, cached only for the active credential."""
    global _user_id_cache, _user_id_cache_key
    config = _require_config(config)
    token_key = hashlib.sha256(config["token"].encode()).digest()
    if _user_id_cache is not None and _user_id_cache_key == token_key:
        return _user_id_cache
    resp = _request("get",
        f"{BASE}/user",
        headers={"Authorization": config["token"], "Content-Type": "application/json"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    _user_id_cache = resp.json()["user"]["id"]
    _user_id_cache_key = token_key
    return _user_id_cache




def _closed_status_name(list_id: str, config=None) -> str:
    """The name of the list's terminal-completed status (varies per list).
    Prefer type=closed over type=done because some lists define multiple
    "done" buckets with distinct meanings — e.g. `abandoned` / `archived` /
    `Closed` — and only the last is real completion. Falling back on
    orderindex would pick 'abandoned' first, which is wrong."""
    config = _require_config(config)
    resp = _request("get",
        f"{BASE}/list/{list_id}", headers=_headers(config), timeout=TIMEOUT
    )
    resp.raise_for_status()
    statuses = resp.json().get("statuses", [])
    for s in statuses:
        if s.get("type") == "closed":
            return s.get("status")
    for s in statuses:
        if s.get("type") == "done":
            return s.get("status")
    return "closed"  # sensible fallback


def close_task(task_id: str, config=None) -> dict:
    """Set a task to its list's closed status."""
    config = _require_config(config)
    task = get_task(task_id, config)
    list_id = (task.get("list") or {}).get("id")
    status_name = _closed_status_name(list_id, config) if list_id else "closed"
    resp = _request("put",
        f"{BASE}/task/{task_id}", headers=_headers(config),
        json={"status": status_name}, timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def link_tasks(task_id: str, links_to: str, config=None) -> dict:
    """Create a ClickUp task-link between two tasks (shows in 'Linked' section)."""
    config = _require_config(config)
    resp = _request("post",
        f"{BASE}/task/{task_id}/link/{links_to}",
        headers=_headers(config), timeout=TIMEOUT
    )
    resp.raise_for_status()
    return resp.json()


def create_review_task(payload: dict, *, config=None) -> dict:
    """Create one Review task from a validated automatic or approved payload."""
    config = _require_config(config)
    draft = payload.get("draft", "")
    body = draft if isinstance(draft, dict) else {"name": str(draft)}
    list_id = payload.get("list_id") or config["create_list_id"]
    if not list_id:
        raise ValueError("no list configured for task creation")
    task = {
        "name": body.get("name", "Untitled"),
        "description": body.get("description", ""),
    }
    try:
        task["assignees"] = [current_user_id(config)]
    except Exception as exc:
        external_errors.log_failure(log, "resolve ClickUp assignee", "clickup", exc)
    labels = body.get("labels") or []
    if labels:
        task["tags"] = [str(tag) for tag in labels]
    resp = _request("post",
        f"{BASE}/list/{list_id}/task",
        headers=_headers(config),
        json=task,
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def execute_action(kind: str, target_id, payload: dict) -> dict:
    """Perform an APPROVED external write. Called only from the actions router."""
    # local-only side effects don't need ClickUp creds. The router handles the
    # actual DB update; we just acknowledge.
    if kind in ("link_mr_to_task", "merge_managed_tasks", "close_orphan_card"):
        return {"local": True}

    config = _require_config()
    draft = payload.get("draft", "")

    if kind == "clickup_comment":
        if not target_id:
            raise ValueError("no target task for comment")
        resp = _request("post",
            f"{BASE}/task/{target_id}/comment",
            headers=_headers(config),
            json={"comment_text": str(draft)},
            timeout=TIMEOUT,
        )
    elif kind == "clickup_status":
        if not target_id:
            raise ValueError("no target task for status change")
        resp = _request("put",
            f"{BASE}/task/{target_id}",
            headers=_headers(config),
            json={"status": str(draft)},
            timeout=TIMEOUT,
        )
    elif kind == "clickup_close_task":
        if not target_id:
            raise ValueError("no target task to close")
        return close_task(target_id, config)
    elif kind == "clickup_create_task":
        created = create_review_task(payload, config=config)
        related = payload.get("related_task_id")
        if related and created.get("id"):
            try:
                link_tasks(created["id"], related, config)
            except requests.RequestException as exc:
                external_errors.log_failure(
                    log,
                    f"link created task {created.get('id')} to related task {related}",
                    "clickup",
                    exc,
                )
        return created
    else:
        raise ValueError(f"unknown action kind: {kind}")

    resp.raise_for_status()
    return resp.json()
