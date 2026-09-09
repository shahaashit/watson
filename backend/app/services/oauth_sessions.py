"""Bounded in-memory state for UI-started OAuth sessions."""

import logging
import threading
import time
import uuid


log = logging.getLogger("watson.oauth")

_MAX_SESSIONS = 20
_COMPLETED_TTL_SECONDS = 30 * 60
_COMPLETED_STATUSES = frozenset(("connected", "failed"))

_sessions: dict[str, dict] = {}
_lock = threading.Lock()


class OAuthSessionCapacityError(RuntimeError):
    """All bounded session slots are occupied by active OAuth flows."""


class OAuthSessionStartError(RuntimeError):
    """An OAuth worker could not start without exposing its original error."""


def _evict_completed_locked(now: float) -> None:
    expired = [
        session_id
        for session_id, record in _sessions.items()
        if record["status"] in _COMPLETED_STATUSES
        and now - record["completed_at"] > _COMPLETED_TTL_SECONDS
    ]
    for session_id in expired:
        _sessions.pop(session_id, None)


def _make_room_locked() -> None:
    while len(_sessions) >= _MAX_SESSIONS:
        completed = [
            (record["completed_at"], session_id)
            for session_id, record in _sessions.items()
            if record["status"] in _COMPLETED_STATUSES
        ]
        if not completed:
            raise OAuthSessionCapacityError("OAuth session capacity reached")
        _, oldest_session_id = min(completed)
        _sessions.pop(oldest_session_id, None)


def _run_google(session_id: str) -> None:
    from ..auth.gcal import run_oauth_flow

    try:
        run_oauth_flow()
        final = {"status": "connected"}
    except Exception:
        log.warning("Google Calendar OAuth session failed")
        final = {
            "status": "failed",
            "error": "Google Calendar connection failed.",
        }

    with _lock:
        record = _sessions.get(session_id)
        if record is not None:
            record.update(final)
            record["completed_at"] = time.monotonic()


def start_google_calendar() -> str:
    """Record a pending session and start one daemon OAuth worker."""
    session_id = uuid.uuid4().hex
    with _lock:
        _evict_completed_locked(time.monotonic())
        _make_room_locked()
        _sessions[session_id] = {
            "session_id": session_id,
            "status": "pending",
        }

    try:
        worker = threading.Thread(
            target=_run_google,
            args=(session_id,),
            name=f"google-oauth-{session_id[:8]}",
            daemon=True,
        )
        worker.start()
    except Exception:
        with _lock:
            _sessions.pop(session_id, None)
        raise OAuthSessionStartError(
            "Unable to start Google Calendar OAuth session."
        ) from None
    return session_id


def status(session_id: str) -> dict:
    """Return a public, credential-free snapshot of an OAuth session."""
    with _lock:
        _evict_completed_locked(time.monotonic())
        try:
            record = _sessions[session_id]
        except KeyError:
            raise KeyError("OAuth session not found") from None
        result = {
            "session_id": record["session_id"],
            "status": record["status"],
        }
        if record["status"] == "failed":
            result["error"] = "Google Calendar connection failed."
        return result
