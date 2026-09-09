"""Cross-integration user metadata, health, and Google URL rewriting.

This module is the single source of truth for "user identity" values Watson
learns at runtime — the OAuth-consenting Google account's email being the
first and most important. Any integration that surfaces a Google URL (Calendar,
Meet, Drive, Docs, Mail, Photos, …) should route it through
`with_authuser_if_google()` so the URL opens in the correct account when the
user has multiple Google identities signed into their browser.

Storage: a tiny key/value table (`user_meta`) in the local SQLite. Integration
health is the exception: its public JSON and whitelisted technical metadata are
stored under separate keys so raw connector exceptions can never reach an API,
audit event, database backup, or frontend.
"""
import json
import re
import threading
from datetime import datetime
from enum import Enum
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from ..models import now_iso
from . import external_errors


# ─── generic get/set on the user_meta table ─────────────────────────────

def get_meta(conn, key: str, default: str = "") -> str:
    row = conn.execute(
        "SELECT value FROM user_meta WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_meta(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO user_meta (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, now_iso()),
    )
    conn.commit()


# ─── Google-specific helpers ────────────────────────────────────────────

GOOGLE_USER_EMAIL_KEY = "google_user_email"


def google_user_email(conn) -> str:
    """Return the OAuth-consenting Google account's email, or "" if none
    has been resolved yet. Populated by gcal_client on every successful sync."""
    return get_meta(conn, GOOGLE_USER_EMAIL_KEY, "")


def set_google_user_email(conn, email: str) -> None:
    """Called by gcal_client after it resolves the user's email from either
    a `self: true` attendee on any event or the primary calendar's `id`."""
    if email:
        set_meta(conn, GOOGLE_USER_EMAIL_KEY, email)


# ─── sync-recency helpers ────────────────────────────────────────────────

LAST_SYNC_AT_KEY = "last_sync_at"


def last_sync_at(conn) -> str:
    """ISO timestamp of the most recent sync pipeline completion (empty if
    Watson has never synced on this DB). Used by the frontend to decide
    whether to auto-trigger a sync when a stale tab regains focus — cron
    ticks don't fire while the Mac is asleep, so a browser opened after
    an overnight sleep sees data from yesterday until we re-sync."""
    return get_meta(conn, LAST_SYNC_AT_KEY, "")


def set_last_sync_at(conn, ts: str = "") -> None:
    """Called by sync_pipeline at the end of every run — regardless of
    per-step success — so the recency signal reflects "we tried recently"
    rather than "everything succeeded recently". Individual failures are
    still visible via server.log for anyone digging in."""
    set_meta(conn, LAST_SYNC_AT_KEY, ts or now_iso())


def set_last_sync_completion(conn, duration_seconds: float, error_count: int) -> None:
    """Publish one completed attempt atomically; never persist error details."""
    ts = now_iso()
    conn.executemany(
        "INSERT INTO user_meta (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        [
            (LAST_SYNC_AT_KEY, ts, ts),
            ("last_sync_duration_seconds", str(duration_seconds), ts),
            ("last_sync_error_count", str(error_count), ts),
        ],
    )
    conn.commit()


def last_sync_status(conn) -> dict:
    """Read completion metrics in one snapshot, including pre-metrics databases."""
    values = dict(conn.execute(
        "SELECT key, value FROM user_meta WHERE key IN (?, ?, ?)",
        (LAST_SYNC_AT_KEY, "last_sync_duration_seconds", "last_sync_error_count"),
    ))
    duration = values.get("last_sync_duration_seconds")
    return {
        "last_sync_at": values.get(LAST_SYNC_AT_KEY) or "",
        "last_sync_duration_seconds": float(duration) if duration else None,
        "last_sync_error_count": int(values.get("last_sync_error_count") or 0),
    }


# ─── structured integration health ──────────────────────────────────────

INTEGRATION_SOURCES = ("clickup", "gitlab", "google-calendar", "flock")
REVIEW_AUTOMATION_SOURCE = "review-automation"
REVIEW_AUTOMATION_COUNT_KEYS = (
    "exact_grouped",
    "ai_grouped",
    "attached",
    "created",
    "deferred",
    "ambiguous",
    "failed",
    "uncertain",
)
_HEALTH_PREFIX = "integration_health:"
_TECHNICAL_HEALTH_PREFIX = "integration_health_technical:"
_HEALTH_STATUSES = {"healthy", "degraded", "disabled", "unconfigured"}
_CACHE_TABLES = {
    "clickup": "clickup_tasks_cache",
    "gitlab": "gitlab_mrs_cache",
    "google-calendar": "gcal_events_cache",
    "flock": "flock_mentions_cache",
}
_HEALTH_LOCK = threading.RLock()
_SENSITIVE_PUBLIC_TEXT = re.compile(
    r"(?i)(authorization|bearer|api[_ -]?key|token|secret|password|pk_|glpat-|xox[baprs]-)"
)


class TechnicalErrorKind(str, Enum):
    EXTERNAL = "external"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    HTTP = "http"
    CREDENTIALS = "credentials"
    CACHE_REFRESH = "cache_refresh"
    CIRCUIT_OPEN = "circuit_open"


class TechnicalOperation(str, Enum):
    CLICKUP_EXACT = "clickup_exact"
    GITLAB_SYNC = "gitlab_sync"
    GCAL_SYNC = "gcal_sync"
    FLOCK_SYNC = "flock_sync"
    CLICKUP_EXACT_RETRY = "clickup_exact_retry"
    GITLAB_RETRY = "gitlab_retry"
    GOOGLE_CALENDAR_RETRY = "google-calendar_retry"
    FLOCK_RETRY = "flock_retry"


_ERROR_KIND_BY_CLASS = {
    "CircuitOpenError": TechnicalErrorKind.CIRCUIT_OPEN,
    "ConnectionError": TechnicalErrorKind.CONNECTION,
    "ConnectTimeout": TechnicalErrorKind.TIMEOUT,
    "FlockRefreshError": TechnicalErrorKind.CACHE_REFRESH,
    "GoogleCredentialError": TechnicalErrorKind.CREDENTIALS,
    "HTTPError": TechnicalErrorKind.HTTP,
    "ProxyError": TechnicalErrorKind.CONNECTION,
    "ReadTimeout": TechnicalErrorKind.TIMEOUT,
    "RequestException": TechnicalErrorKind.HTTP,
    "SecretStoreError": TechnicalErrorKind.CREDENTIALS,
    "SSLError": TechnicalErrorKind.CONNECTION,
    "Timeout": TechnicalErrorKind.TIMEOUT,
    "TimeoutError": TechnicalErrorKind.TIMEOUT,
}
_OPERATION_BY_NAME = {item.value: item for item in TechnicalOperation}


def technical_metadata(*, error_type: str = "", operation: str = "") -> dict:
    """Map trusted connector labels onto closed internal enums.

    This is intentionally separate from ``set_integration_health``: arbitrary
    dictionaries at the persistence boundary are never promoted into trusted
    metadata merely because their strings look harmless.
    """
    metadata = {}
    if error_type:
        metadata["error_type"] = _ERROR_KIND_BY_CLASS.get(
            error_type, TechnicalErrorKind.EXTERNAL
        )
    mapped_operation = _OPERATION_BY_NAME.get(operation)
    if mapped_operation is not None:
        metadata["operation"] = mapped_operation
    return metadata


def _now() -> datetime:
    return datetime.now()


def _health_key(source: str) -> str:
    return _HEALTH_PREFIX + source


def _technical_health_key(source: str) -> str:
    return _TECHNICAL_HEALTH_PREFIX + source


def _stored_public_health(conn, source: str) -> dict:
    raw = get_meta(conn, _health_key(source), "")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _public_message(source: str, value) -> str:
    message = " ".join(str(value or "").split())[:240]
    if message and not _SENSITIVE_PUBLIC_TEXT.search(message):
        return message
    return external_errors.message(source) if message else ""


def _safe_technical_metadata(health: dict) -> dict[str, str]:
    """Serialize only closed enum values produced by trusted call sites."""
    supplied = health.get("technical")
    if not isinstance(supplied, dict):
        supplied = {}
    error = health.get("technical_error")
    if isinstance(error, BaseException):
        supplied = {
            **supplied,
            "error_type": _ERROR_KIND_BY_CLASS.get(
                external_errors.exception_type(error), TechnicalErrorKind.EXTERNAL
            ),
        }
    out = {}
    error_kind = supplied.get("error_type")
    if isinstance(error_kind, TechnicalErrorKind):
        out["error_type"] = error_kind.value
    operation = supplied.get("operation")
    if isinstance(operation, TechnicalOperation):
        out["operation"] = operation.value
    return out


def set_integration_health(conn, source: str, health: dict) -> None:
    """Atomically persist public health and separate safe technical metadata.

    ``technical_error`` is accepted at this boundary so callers cannot
    accidentally serialize it; string values are deliberately discarded.
    A real exception contributes only its mapped internal error category.
    """
    if source not in INTEGRATION_SOURCES:
        raise ValueError("unknown integration source")
    if not isinstance(health, dict):
        raise TypeError("integration health must be a dictionary")
    status = str(health.get("status") or "")
    if status not in _HEALTH_STATUSES:
        raise ValueError("invalid integration health status")

    technical = _safe_technical_metadata(health)

    with _HEALTH_LOCK:
        # Merge under the same lock as the write: a degraded result completing
        # after a success must retain that newly-recorded success timestamp.
        previous = _stored_public_health(conn, source)
        public = {
            "status": status,
            "last_success_at": str(
                health.get(
                    "last_success_at", previous.get("last_success_at", "")
                ) or ""
            ),
            "retry_at": str(health.get("retry_at", "") or ""),
            "message": _public_message(source, health.get("message", "")),
        }
        stamp = now_iso()
        try:
            conn.execute(
                "INSERT INTO user_meta (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (_health_key(source), json.dumps(public, sort_keys=True), stamp),
            )
            if technical:
                conn.execute(
                    "INSERT INTO user_meta (key, value, updated_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (
                        _technical_health_key(source),
                        json.dumps(technical, sort_keys=True),
                        stamp,
                    ),
                )
            else:
                conn.execute(
                    "DELETE FROM user_meta WHERE key=?",
                    (_technical_health_key(source),),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _source_disabled(conn, source: str) -> bool:
    row = conn.execute(
        "SELECT value_json FROM app_settings WHERE key=?",
        (f"integration.{source}.disabled",),
    ).fetchone()
    if row is None:
        return False
    try:
        return json.loads(row["value_json"]) is True
    except (TypeError, json.JSONDecodeError):
        return True


def _cached_age_seconds(conn, source: str, fallback_at: str = ""):
    table = _CACHE_TABLES[source]
    row = conn.execute(f"SELECT MAX(synced_at) AS cached_at FROM {table}").fetchone()
    cached_at = (row["cached_at"] if row else None) or fallback_at
    if not cached_at:
        return None
    try:
        cached = datetime.fromisoformat(str(cached_at).replace("Z", "+00:00"))
        current = _now()
        if cached.tzinfo is not None and current.tzinfo is None:
            current = current.astimezone()
        elif cached.tzinfo is None and current.tzinfo is not None:
            cached = cached.replace(tzinfo=current.tzinfo)
        return max(0, int((current - cached).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return None


def integration_health(conn) -> list[dict]:
    """Public health for every connector; technical metadata stays private."""
    out = []
    for source in INTEGRATION_SOURCES:
        stored = _stored_public_health(conn, source)
        if _source_disabled(conn, source):
            status = "disabled"
            message = "Integration is disabled in Settings."
        elif not stored:
            status = "unconfigured"
            message = "Integration is not configured. Open Settings to connect it."
        else:
            status = stored.get("status", "unconfigured")
            message = stored.get("message", "")
        out.append({
            "source": source,
            "status": status,
            "last_success_at": stored.get("last_success_at", ""),
            "cached_age_seconds": _cached_age_seconds(
                conn, source, stored.get("last_success_at", "")
            ),
            "retry_at": stored.get("retry_at", ""),
            "message": message,
        })
    review = _stored_public_health(conn, REVIEW_AUTOMATION_SOURCE)
    if review:
        out.append({
            "source": REVIEW_AUTOMATION_SOURCE,
            "status": review.get("status", "degraded"),
            "last_attempt_at": review.get("last_attempt_at", ""),
            "last_success_at": review.get("last_success_at", ""),
            "cached_age_seconds": None,
            "retry_at": "",
            "message": review.get("message", ""),
            "counts": {
                key: int(review.get("counts", {}).get(key, 0) or 0)
                for key in REVIEW_AUTOMATION_COUNT_KEYS
            },
        })
    return out


def set_review_automation_health(conn, result: dict) -> None:
    """Cache only safe review-stage state and non-negative numeric counts."""
    previous = _stored_public_health(conn, REVIEW_AUTOMATION_SOURCE)
    counts = {
        key: max(0, int(result.get(key, 0) or 0))
        if isinstance(result.get(key, 0), int)
        else 0
        for key in REVIEW_AUTOMATION_COUNT_KEYS
    }
    attention = counts["deferred"] + counts["failed"] + counts["uncertain"]
    failed_step = isinstance(result, dict) and "error" in result
    status = "degraded" if attention or failed_step else "healthy"
    message = (
        "Review automation needs attention. Open My Work to resolve it."
        if status == "degraded"
        else ""
    )
    stamp = now_iso()
    public = {
        "status": status,
        "last_attempt_at": stamp,
        "last_success_at": (
            previous.get("last_success_at", "") if status == "degraded" else stamp
        ),
        "message": message,
        "counts": counts,
    }
    with _HEALTH_LOCK:
        conn.execute(
            "INSERT INTO user_meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (
                _health_key(REVIEW_AUTOMATION_SOURCE),
                json.dumps(public, sort_keys=True),
                stamp,
            ),
        )
        conn.commit()


def with_authuser_if_google(url: str, email: str) -> str:
    """Append/replace `authuser=<email>` on any Google-owned URL.

    Non-Google URLs pass through unchanged. Google URLs (host is `google.com`
    or any `*.google.com` subdomain — meet, calendar, docs, drive, mail,
    photos, hangouts, meet.google.com, etc.) get the query param set so the
    browser opens the URL in the specified account's profile.

    Safe to call with any URL and any (possibly empty) email — a missing
    email is treated as a no-op.
    """
    if not url or not email:
        return url
    try:
        parts = urlparse(url)
    except ValueError:
        return url
    host = (parts.netloc or "").lower()
    # Strip any user-info / port for the domain check.
    host = host.split("@")[-1].split(":")[0]
    if host != "google.com" and not host.endswith(".google.com"):
        return url
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q["authuser"] = email
    return urlunparse(parts._replace(query=urlencode(q)))
