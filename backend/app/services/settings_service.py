"""Permanent Settings, onboarding, and people configuration.

Only the source-specific functions in this module can translate API payloads
into application setting keys. Credentials cross the ``secret_store`` boundary
and are never serialized into an API response or SQLite row.
"""

import queue
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

from ..config import Settings, settings
from ..models import now_iso, person_dict
from ..schemas import (
    AnthropicSettingsIn,
    ClickUpSettingsIn,
    FlockSettingsIn,
    GitLabSettingsIn,
    GoogleCalendarSettingsIn,
)
from . import app_settings, secret_store, work_items


SOURCES = ("anthropic", "gitlab", "clickup", "google-calendar", "flock")
SOURCE_MODELS: dict[str, type[BaseModel]] = {
    "anthropic": AnthropicSettingsIn,
    "gitlab": GitLabSettingsIn,
    "clickup": ClickUpSettingsIn,
    "google-calendar": GoogleCalendarSettingsIn,
    "flock": FlockSettingsIn,
}

_SOURCE_SETTING_KEYS = {
    "anthropic": (
        "integration.anthropic.base_url",
        "integration.anthropic.model",
    ),
    "gitlab": (
        "integration.gitlab.base_url",
        "integration.gitlab.username",
        "integration.gitlab.projects",
    ),
    "clickup": ("integration.clickup.create_list_id",),
    "google-calendar": (),
    "flock": (
        "integration.flock.profile_dir",
        "integration.flock.user_handle",
    ),
}

_SOURCE_SECRET_KEYS = {
    "anthropic": ("anthropic.api_key",),
    "gitlab": ("gitlab.token",),
    "clickup": ("clickup.token",),
    "google-calendar": ("google.client_config", "google.authorized_user"),
    "flock": (),
}
_CONNECTION_TEST_TIMEOUT_SECONDS = 20.0
_DEFAULT_PEOPLE_EMAIL_DOMAIN = "example.com"
_EMAIL_DOMAIN_RE = re.compile(
    r"(?i)^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}$"
)


class ConnectionTestBusy(RuntimeError):
    """The source already has a still-running synchronous connection test."""


class ProfileApplyError(RuntimeError):
    """A validated profile update could not be applied atomically."""


@dataclass
class _ConnectionTestRun:
    worker: threading.Thread
    result_queue: queue.Queue


_connection_test_lock = threading.Lock()
_connection_test_runs: dict[str, _ConnectionTestRun] = {}


def _fresh_environment() -> Settings:
    """Read current environment/.env fallbacks without reusing runtime overrides."""
    return Settings()


def _disabled(conn, source: str) -> bool:
    return bool(app_settings.get(conn, f"integration.{source}.disabled", False))


def _safe_effective_secret(name: str, fallback: str) -> str:
    try:
        return secret_store.effective_secret(name, fallback)
    except secret_store.SecretStoreError:
        return ""


def _secret_state(name: str, fallback: str = "") -> tuple[bool, str, bool]:
    """Return presence/value/unavailable while treating Keychain errors as fatal."""
    try:
        present = secret_store.has_secret(name)
        effective = secret_store.effective_secret(name, fallback)
    except secret_store.SecretStoreError:
        return False, "", True
    return present, effective, False


def _secret_value(value) -> str | None:
    return value.get_secret_value() if value is not None else None


def _effective(conn, key: str, fallback):
    return app_settings.get(conn, key, fallback)


def apply_runtime_settings(conn) -> None:
    """Refresh connector-facing settings after boot or a Settings mutation."""
    fallback = _fresh_environment()
    settings.watson_user_name = _effective(
        conn, "profile.display_name", fallback.watson_user_name
    )
    settings.tz = _effective(conn, "profile.timezone", fallback.tz)

    settings.anthropic_base_url = _effective(
        conn, "integration.anthropic.base_url", fallback.anthropic_base_url
    )
    settings.anthropic_model = _effective(
        conn, "integration.anthropic.model", fallback.anthropic_model
    )
    settings.gitlab_base_url = _effective(
        conn, "integration.gitlab.base_url", fallback.gitlab_base_url
    )
    settings.gitlab_username = _effective(
        conn, "integration.gitlab.username", fallback.gitlab_username
    )
    settings.clickup_create_list_id = _effective(
        conn, "integration.clickup.create_list_id", fallback.clickup_create_list_id
    )
    if app_settings.get(conn, "integration.clickup.create_list_id") is not None:
        settings.clickup_list_ids = settings.clickup_create_list_id
    else:
        settings.clickup_list_ids = fallback.clickup_list_ids
    settings.flock_profile_dir = _effective(
        conn, "integration.flock.profile_dir", fallback.flock_profile_dir
    )
    settings.flock_user_handle = _effective(
        conn, "integration.flock.user_handle", fallback.flock_user_handle
    )

    settings.anthropic_api_key = (
        ""
        if _disabled(conn, "anthropic")
        else _safe_effective_secret("anthropic.api_key", fallback.anthropic_api_key)
    )
    settings.gitlab_token = (
        ""
        if _disabled(conn, "gitlab")
        else _safe_effective_secret("gitlab.token", fallback.gitlab_token)
    )
    settings.clickup_api_token = (
        ""
        if _disabled(conn, "clickup")
        else _safe_effective_secret("clickup.token", fallback.clickup_api_token)
    )


def _health(conn, source: str):
    return app_settings.get(conn, f"integration.{source}.health")


def _common_view(
    conn,
    source: str,
    *,
    credential_present: bool,
    configured: bool,
    credential_unavailable: bool = False,
):
    configured = bool(configured and not _disabled(conn, source))
    return {
        "source": source,
        "credential_present": credential_present,
        "configured": configured,
        "health": (
            {
                "status": "unavailable",
                "error": "Credential store unavailable.",
            }
            if credential_unavailable
            else _health(conn, source)
        ),
        "reconnect_required": not configured,
    }


def _anthropic_view(conn, fallback: Settings) -> dict:
    credential_present, effective_key, unavailable = _secret_state(
        "anthropic.api_key", fallback.anthropic_api_key
    )
    return {
        "source": "anthropic",
        "base_url": _effective(
            conn, "integration.anthropic.base_url", fallback.anthropic_base_url
        ),
        "model": _effective(
            conn, "integration.anthropic.model", fallback.anthropic_model
        ),
        **{
            key: value
            for key, value in _common_view(
                conn,
                "anthropic",
                credential_present=credential_present,
                configured=bool(effective_key),
                credential_unavailable=unavailable,
            ).items()
            if key != "source"
        },
    }


def _gitlab_view(conn, fallback: Settings) -> dict:
    base_url = _effective(
        conn, "integration.gitlab.base_url", fallback.gitlab_base_url
    )
    credential_present, effective_token, unavailable = _secret_state(
        "gitlab.token", fallback.gitlab_token
    )
    return {
        "source": "gitlab",
        "base_url": base_url,
        "username": _effective(
            conn, "integration.gitlab.username", fallback.gitlab_username
        ),
        **{
            key: value
            for key, value in _common_view(
                conn,
                "gitlab",
                credential_present=credential_present,
                configured=bool(base_url and effective_token),
                credential_unavailable=unavailable,
            ).items()
            if key != "source"
        },
    }


def _clickup_view(conn, fallback: Settings) -> dict:
    create_list_id = _effective(
        conn, "integration.clickup.create_list_id", fallback.clickup_create_list
    )
    credential_present, effective_token, unavailable = _secret_state(
        "clickup.token", fallback.clickup_api_token
    )
    return {
        "source": "clickup",
        "create_list_id": create_list_id,
        **{
            key: value
            for key, value in _common_view(
                conn,
                "clickup",
                credential_present=credential_present,
                configured=bool(create_list_id and effective_token),
                credential_unavailable=unavailable,
            ).items()
            if key != "source"
        },
    }


def _google_calendar_view(conn, fallback: Settings) -> dict:
    client_present, _, client_unavailable = _secret_state("google.client_config")
    authorized, _, authorized_unavailable = _secret_state("google.authorized_user")
    unavailable = client_unavailable or authorized_unavailable
    return _common_view(
        conn,
        "google-calendar",
        credential_present=client_present,
        configured=not unavailable and client_present and authorized,
        credential_unavailable=unavailable,
    )


def _flock_view(conn, fallback: Settings) -> dict:
    from . import flock_client

    profile_dir = _effective(
        conn, "integration.flock.profile_dir", fallback.flock_profile_dir
    )
    profile = Path(profile_dir).expanduser() if profile_dir else fallback.flock_profile_path
    try:
        configured = flock_client.probe_authenticated_session(profile)
    except Exception:
        configured = False
    return {
        "source": "flock",
        "profile_dir": profile_dir,
        "user_handle": _effective(
            conn, "integration.flock.user_handle", fallback.flock_user_handle
        ),
        **{
            key: value
            for key, value in _common_view(
                conn,
                "flock",
                credential_present=False,
                configured=configured,
            ).items()
            if key != "source"
        },
    }


def integration_view(conn, source: str) -> dict:
    fallback = _fresh_environment()
    handlers = {
        "anthropic": _anthropic_view,
        "gitlab": _gitlab_view,
        "clickup": _clickup_view,
        "google-calendar": _google_calendar_view,
        "flock": _flock_view,
    }
    try:
        handler = handlers[source]
    except KeyError:
        raise ValueError("unsupported integration source") from None
    return handler(conn, fallback)


def profile(conn) -> dict:
    fallback = _fresh_environment()
    return {
        "display_name": _effective(
            conn, "profile.display_name", fallback.watson_user_name
        ),
        "timezone": _effective(conn, "profile.timezone", fallback.tz),
        "email_domain": _people_email_domain(conn, fallback),
        "separate_work_by_status": bool(
            app_settings.get(conn, "profile.separate_work_by_status", False)
        ),
        "auto_create_review_tasks": bool(
            app_settings.get(conn, "profile.auto_create_review_tasks", True)
        ),
        "ai_group_review_mrs": bool(
            app_settings.get(conn, "profile.ai_group_review_mrs", True)
        ),
    }


def onboarding(conn) -> dict:
    return {
        "completed": bool(app_settings.get(conn, "onboarding.completed", False)),
        "step": app_settings.get(conn, "onboarding.step", 1),
    }


def data_settings(conn) -> dict:
    fallback = _fresh_environment()
    configured = app_settings.get(conn, "data.backup_dir")
    backup_dir = (
        Path(configured).expanduser().resolve()
        if configured
        else fallback.backup_dir.resolve()
    )
    return {"backup_dir": str(backup_dir)}


def settings_snapshot(conn) -> dict:
    return {
        "profile": profile(conn),
        "integrations": {
            source: integration_view(conn, source) for source in SOURCES
        },
        "data": data_settings(conn),
        "onboarding": onboarding(conn),
    }


def _reschedule_timezone(timezone: str) -> None:
    from ..main import reschedule_scheduler_timezone

    reschedule_scheduler_timezone(timezone)


def update_profile(
    conn,
    *,
    display_name: str | None,
    timezone: str | None,
    email_domain: str | None = None,
    separate_work_by_status: bool | None = None,
    auto_create_review_tasks: bool | None = None,
    ai_group_review_mrs: bool | None = None,
) -> dict:
    display_name = display_name.strip() if display_name is not None else None
    timezone = timezone.strip() if timezone is not None else None
    email_domain = email_domain.strip().lower() if email_domain is not None else None
    if display_name is not None and not display_name:
        raise ValueError("display_name must not be blank")
    if timezone is not None:
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown timezone") from None
    if email_domain is not None and not _EMAIL_DOMAIN_RE.fullmatch(email_domain):
        raise ValueError("email_domain must be a valid domain")
    previous_runtime_timezone = settings.tz
    previous_runtime_name = settings.watson_user_name
    previous_timezone_setting = conn.execute(
        "SELECT value_json, updated_at FROM app_settings WHERE key=?",
        ("profile.timezone",),
    ).fetchone()
    previous_name_setting = conn.execute(
        "SELECT value_json, updated_at FROM app_settings WHERE key=?",
        ("profile.display_name",),
    ).fetchone()
    previous_email_domain_setting = conn.execute(
        "SELECT value_json, updated_at FROM app_settings WHERE key=?",
        ("profile.email_domain",),
    ).fetchone()
    previous_email_identities = conn.execute(
        "SELECT id, person_id, source, external_id, display_value "
        "FROM person_identities "
        "WHERE source IN ('clickup', 'google-calendar', 'flock') ORDER BY id"
    ).fetchall()
    previous_board_setting = conn.execute(
        "SELECT value_json, updated_at FROM app_settings WHERE key=?",
        ("profile.separate_work_by_status",),
    ).fetchone()
    previous_auto_create_review_tasks_setting = conn.execute(
        "SELECT value_json, updated_at FROM app_settings WHERE key=?",
        ("profile.auto_create_review_tasks",),
    ).fetchone()
    previous_ai_group_review_mrs_setting = conn.execute(
        "SELECT value_json, updated_at FROM app_settings WHERE key=?",
        ("profile.ai_group_review_mrs",),
    ).fetchone()
    previous_self = conn.execute(
        "SELECT id, display_name, updated_at FROM people WHERE is_self=1"
    ).fetchone()
    previous_timezone = profile(conn)["timezone"]
    mutation_committed = False
    try:
        conn.execute("BEGIN")
        if timezone is not None:
            app_settings.set_value(
                conn, "profile.timezone", timezone, commit=False
            )
        if display_name is not None:
            app_settings.set_value(
                conn, "profile.display_name", display_name, commit=False
            )
            self_id = work_items.ensure_self_person(conn, display_name)
            conn.execute(
                "UPDATE people SET display_name=?, updated_at=? WHERE id=?",
                (display_name, now_iso(), self_id),
            )
        if email_domain is not None:
            app_settings.set_value(
                conn, "profile.email_domain", email_domain, commit=False
            )
            _regenerate_tracked_email_identities(conn)
        if separate_work_by_status is not None:
            app_settings.set_value(
                conn,
                "profile.separate_work_by_status",
                separate_work_by_status,
                commit=False,
            )
        if auto_create_review_tasks is not None:
            app_settings.set_value(
                conn,
                "profile.auto_create_review_tasks",
                auto_create_review_tasks,
                commit=False,
            )
        if ai_group_review_mrs is not None:
            app_settings.set_value(
                conn,
                "profile.ai_group_review_mrs",
                ai_group_review_mrs,
                commit=False,
            )
        conn.commit()
        mutation_committed = True
        apply_runtime_settings(conn)
        if timezone is not None and timezone != previous_timezone:
            _reschedule_timezone(timezone)
    except Exception:
        conn.rollback()
        if mutation_committed:
            try:
                conn.execute("BEGIN")
                if previous_timezone_setting is None:
                    app_settings.delete(
                        conn, "profile.timezone", commit=False
                    )
                else:
                    conn.execute(
                        "INSERT INTO app_settings (key, value_json, updated_at)"
                        " VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET"
                        " value_json=excluded.value_json, updated_at=excluded.updated_at",
                        (
                            "profile.timezone",
                            previous_timezone_setting["value_json"],
                            previous_timezone_setting["updated_at"],
                        ),
                    )
                if previous_name_setting is None:
                    app_settings.delete(
                        conn, "profile.display_name", commit=False
                    )
                else:
                    conn.execute(
                        "INSERT INTO app_settings (key, value_json, updated_at)"
                        " VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET"
                        " value_json=excluded.value_json, updated_at=excluded.updated_at",
                        (
                            "profile.display_name",
                            previous_name_setting["value_json"],
                            previous_name_setting["updated_at"],
                        ),
                    )
                if previous_email_domain_setting is None:
                    app_settings.delete(
                        conn, "profile.email_domain", commit=False
                    )
                else:
                    conn.execute(
                        "INSERT INTO app_settings (key, value_json, updated_at)"
                        " VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET"
                        " value_json=excluded.value_json, updated_at=excluded.updated_at",
                        (
                            "profile.email_domain",
                            previous_email_domain_setting["value_json"],
                            previous_email_domain_setting["updated_at"],
                        ),
                    )
                if email_domain is not None:
                    conn.execute(
                        "DELETE FROM person_identities "
                        "WHERE source IN ('clickup', 'google-calendar', 'flock')"
                    )
                    conn.executemany(
                        "INSERT INTO person_identities "
                        "(id, person_id, source, external_id, display_value) "
                        "VALUES (?, ?, ?, ?, ?)",
                        [tuple(row) for row in previous_email_identities],
                    )
                if previous_board_setting is None:
                    app_settings.delete(
                        conn, "profile.separate_work_by_status", commit=False
                    )
                else:
                    conn.execute(
                        "INSERT INTO app_settings (key, value_json, updated_at)"
                        " VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET"
                        " value_json=excluded.value_json, updated_at=excluded.updated_at",
                        (
                            "profile.separate_work_by_status",
                            previous_board_setting["value_json"],
                            previous_board_setting["updated_at"],
                        ),
                    )
                if previous_auto_create_review_tasks_setting is None:
                    app_settings.delete(
                        conn, "profile.auto_create_review_tasks", commit=False
                    )
                else:
                    conn.execute(
                        "INSERT INTO app_settings (key, value_json, updated_at)"
                        " VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET"
                        " value_json=excluded.value_json, updated_at=excluded.updated_at",
                        (
                            "profile.auto_create_review_tasks",
                            previous_auto_create_review_tasks_setting["value_json"],
                            previous_auto_create_review_tasks_setting["updated_at"],
                        ),
                    )
                if previous_ai_group_review_mrs_setting is None:
                    app_settings.delete(
                        conn, "profile.ai_group_review_mrs", commit=False
                    )
                else:
                    conn.execute(
                        "INSERT INTO app_settings (key, value_json, updated_at)"
                        " VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET"
                        " value_json=excluded.value_json, updated_at=excluded.updated_at",
                        (
                            "profile.ai_group_review_mrs",
                            previous_ai_group_review_mrs_setting["value_json"],
                            previous_ai_group_review_mrs_setting["updated_at"],
                        ),
                    )
                if previous_self is None:
                    conn.execute("DELETE FROM people WHERE is_self=1")
                else:
                    conn.execute(
                        "UPDATE people SET display_name=?, updated_at=? WHERE id=?",
                        (
                            previous_self["display_name"],
                            previous_self["updated_at"],
                            previous_self["id"],
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise ProfileApplyError("profile compensation failed") from None
            finally:
                apply_runtime_settings(conn)
                settings.tz = previous_runtime_timezone
                settings.watson_user_name = previous_runtime_name
        raise ProfileApplyError("profile update failed") from None
    return profile(conn)


def update_onboarding(
    conn, *, completed: bool | None, step: int | None
) -> dict:
    if completed is not None:
        app_settings.set_value(conn, "onboarding.completed", completed)
    if step is not None:
        app_settings.set_value(conn, "onboarding.step", step)
    return onboarding(conn)


def _save_anthropic(conn, payload: AnthropicSettingsIn) -> None:
    app_settings.set_value(
        conn, "integration.anthropic.base_url", payload.base_url, commit=False
    )
    app_settings.set_value(
        conn, "integration.anthropic.model", payload.model, commit=False
    )


def _save_gitlab(conn, payload: GitLabSettingsIn) -> None:
    app_settings.set_value(
        conn, "integration.gitlab.base_url", payload.base_url, commit=False
    )
    app_settings.set_value(
        conn, "integration.gitlab.username", payload.username, commit=False
    )
    if app_settings.get(conn, "integration.gitlab.projects") is None:
        app_settings.set_value(
            conn, "integration.gitlab.projects", [], commit=False
        )


def gitlab_projects(conn) -> list[dict]:
    projects = app_settings.get(conn, "integration.gitlab.projects", [])
    if not isinstance(projects, list):
        return []
    return [
        project
        for project in projects
        if isinstance(project, dict)
        and isinstance(project.get("id"), int)
        and project.get("id") > 0
        and isinstance(project.get("path"), str)
    ]


def known_gitlab_projects(conn) -> list[dict]:
    """Repository choices already proven by cached MRs or prior selection."""
    by_id = {project["id"]: project for project in gitlab_projects(conn)}
    for row in conn.execute(
        "SELECT DISTINCT mr_id, project FROM gitlab_mrs_cache "
        "WHERE TRIM(project) <> '' ORDER BY project COLLATE NOCASE"
    ):
        project_id = str(row["mr_id"] or "").split("!", 1)[0]
        if not project_id.isdigit():
            continue
        path = str(row["project"] or "").strip()
        by_id.setdefault(
            int(project_id),
            {"id": int(project_id), "name": path.rsplit("/", 1)[-1], "path": path},
        )
    return sorted(by_id.values(), key=lambda project: project["path"].casefold())


def update_gitlab_projects(conn, project_ids: list[int]) -> list[dict]:
    from . import gitlab_client

    selected_ids = set(project_ids)
    known = {project["id"]: project for project in known_gitlab_projects(conn)}
    try:
        for project_id in selected_ids - set(known):
            known[project_id] = gitlab_client.get_accessible_project(project_id)
    except Exception:
        raise ValueError("One or more GitLab projects are not accessible.") from None
    selected = sorted(
        (known[project_id] for project_id in selected_ids),
        key=lambda project: project["path"].casefold(),
    )
    app_settings.set_value(conn, "integration.gitlab.projects", selected)
    return selected


def _save_clickup(conn, payload: ClickUpSettingsIn) -> None:
    app_settings.set_value(
        conn,
        "integration.clickup.create_list_id",
        payload.create_list_id,
        commit=False,
    )


def _save_google_calendar(conn, payload: GoogleCalendarSettingsIn) -> None:
    return None


def _save_flock(conn, payload: FlockSettingsIn) -> None:
    app_settings.set_value(
        conn, "integration.flock.profile_dir", payload.profile_dir, commit=False
    )
    app_settings.set_value(
        conn, "integration.flock.user_handle", payload.user_handle, commit=False
    )


def _payload_secret_updates(source: str, payload: BaseModel) -> dict[str, str]:
    if source == "anthropic":
        value = _secret_value(payload.api_key)
        return {"anthropic.api_key": value} if value is not None else {}
    if source == "gitlab":
        value = _secret_value(payload.token)
        return {"gitlab.token": value} if value is not None else {}
    if source == "clickup":
        value = _secret_value(payload.token)
        return {"clickup.token": value} if value is not None else {}
    if source == "google-calendar":
        value = _secret_value(payload.client_config_json)
        return {"google.client_config": value} if value is not None else {}
    return {}


def _persist_disabled_tombstone(conn, source: str) -> None:
    app_settings.set_value(conn, f"integration.{source}.disabled", True)
    apply_runtime_settings(conn)


def _quarantine_managed_flock_profile(conn, fallback: Settings) -> dict[str, str]:
    """Move only Watson's canonical Flock profile into local quarantine.

    The configured value is untrusted settings data.  Resolve it and require
    an exact match with ``<data_dir>/chrome-profile-flock``; a nested path,
    symlink escape, legacy custom directory, or regular file is never touched.
    """
    guidance = {
        "quarantined": (
            "The Watson-managed browser profile was moved to local quarantine."
        ),
        "not_found": "No Watson-managed browser profile was present.",
        "unsafe_profile_left_in_place": (
            "The configured profile was not Watson-managed; it was left untouched. "
            "Review it manually before removing anything."
        ),
        "quarantine_failed": (
            "The browser profile could not be quarantined and was left untouched. "
            "The integration remains disabled; review the profile manually."
        ),
    }
    configured = _effective(
        conn, "integration.flock.profile_dir", fallback.flock_profile_dir
    )
    data_dir = settings.data_dir.expanduser()
    candidate = Path(configured).expanduser() if configured else (
        data_dir / "chrome-profile-flock"
    )
    expected = data_dir / "chrome-profile-flock"
    try:
        resolved_data = data_dir.resolve()
        resolved_candidate = candidate.resolve()
        resolved_expected = expected.resolve()
    except (OSError, RuntimeError):
        state = "unsafe_profile_left_in_place"
        return {"disconnect_state": state, "disconnect_guidance": guidance[state]}
    if (
        candidate.is_symlink()
        or resolved_candidate != resolved_expected
        or resolved_candidate.parent != resolved_data
    ):
        state = "unsafe_profile_left_in_place"
        return {"disconnect_state": state, "disconnect_guidance": guidance[state]}
    if not candidate.exists():
        state = "not_found"
        return {"disconnect_state": state, "disconnect_guidance": guidance[state]}
    if not candidate.is_dir():
        state = "unsafe_profile_left_in_place"
        return {"disconnect_state": state, "disconnect_guidance": guidance[state]}
    try:
        quarantine_root = resolved_data / "disconnected-profiles"
        quarantine_root.mkdir(parents=True, exist_ok=True)
        if quarantine_root.is_symlink() or quarantine_root.resolve().parent != resolved_data:
            raise OSError("unsafe quarantine root")
        destination = quarantine_root / f"flock-{uuid.uuid4().hex}"
        candidate.rename(destination)
    except OSError:
        state = "quarantine_failed"
        return {"disconnect_state": state, "disconnect_guidance": guidance[state]}
    state = "quarantined"
    return {"disconnect_state": state, "disconnect_guidance": guidance[state]}


def update_integration(
    conn,
    source: str,
    payload: BaseModel,
    *,
    extra_secret_updates: dict[str, str] | None = None,
) -> dict:
    handlers = {
        "anthropic": _save_anthropic,
        "gitlab": _save_gitlab,
        "clickup": _save_clickup,
        "google-calendar": _save_google_calendar,
        "flock": _save_flock,
    }
    try:
        handler = handlers[source]
    except KeyError:
        raise ValueError("unsupported integration source") from None
    secret_updates = _payload_secret_updates(source, payload)
    secret_updates.update(extra_secret_updates or {})
    _persist_disabled_tombstone(conn, source)
    try:
        conn.execute("BEGIN")
        handler(conn, payload)
        app_settings.delete(
            conn, f"integration.{source}.health", commit=False
        )
        for name, value in secret_updates.items():
            secret_store.set_secret(name, value)
        # Even a host-only edit must prove the existing credential store is
        # readable before it can pair new non-secrets with an old credential.
        for name in _SOURCE_SECRET_KEYS[source]:
            secret_store.has_secret(name)
        app_settings.delete(
            conn, f"integration.{source}.disabled", commit=False
        )
        conn.commit()
    except Exception:
        conn.rollback()
        apply_runtime_settings(conn)
        raise
    apply_runtime_settings(conn)
    return integration_view(conn, source)


def disconnect_integration(conn, source: str) -> dict:
    if source not in SOURCES:
        raise ValueError("unsupported integration source")
    fallback = _fresh_environment()
    _persist_disabled_tombstone(conn, source)
    disconnect_details = (
        _quarantine_managed_flock_profile(conn, fallback)
        if source == "flock"
        else {}
    )
    try:
        conn.execute("BEGIN")
        for key in _SOURCE_SETTING_KEYS[source]:
            app_settings.delete(conn, key, commit=False)
        app_settings.delete(
            conn, f"integration.{source}.health", commit=False
        )
        for name in _SOURCE_SECRET_KEYS[source]:
            secret_store.delete_secret(name)
        conn.commit()
    except Exception:
        conn.rollback()
        apply_runtime_settings(conn)
        raise
    apply_runtime_settings(conn)
    return {**integration_view(conn, source), **disconnect_details}


def import_environment(conn, source: str) -> dict:
    fallback = _fresh_environment()
    extra_secret_updates = {}
    if source == "anthropic":
        payload = AnthropicSettingsIn(
            api_key=fallback.anthropic_api_key or None,
            base_url=fallback.anthropic_base_url,
            model=fallback.anthropic_model,
        )
    elif source == "gitlab":
        payload = GitLabSettingsIn(
            base_url=fallback.gitlab_base_url,
            token=fallback.gitlab_token or None,
            username=fallback.gitlab_username,
        )
    elif source == "clickup":
        payload = ClickUpSettingsIn(
            token=fallback.clickup_api_token or None,
            create_list_id=fallback.clickup_create_list,
        )
    elif source == "google-calendar":
        client_json = (
            fallback.gcal_credentials_file.read_text()
            if fallback.gcal_credentials_file.is_file()
            else None
        )
        payload = GoogleCalendarSettingsIn(client_config_json=client_json)
        if fallback.gcal_token_file.is_file():
            extra_secret_updates["google.authorized_user"] = (
                fallback.gcal_token_file.read_text()
            )
    elif source == "flock":
        payload = FlockSettingsIn(
            profile_dir=fallback.flock_profile_dir,
            user_handle=fallback.flock_user_handle,
        )
    else:
        raise ValueError("unsupported integration source")
    return update_integration(
        conn, source, payload, extra_secret_updates=extra_secret_updates
    )


def _upsert_self_gitlab_identity(conn, user: dict) -> None:
    username = str(user.get("username") or "").strip()
    if not username:
        raise ValueError("GitLab did not return a username")
    self_id = work_items.ensure_self_person(conn, profile(conn)["display_name"])
    conn.execute(
        "INSERT INTO person_identities (person_id, source, external_id, display_value) "
        "VALUES (?, 'gitlab', ?, ?) "
        "ON CONFLICT(source, external_id) DO UPDATE SET "
        "person_id=excluded.person_id, display_value=excluded.display_value",
        (self_id, username, str(user.get("name") or username)),
    )
    app_settings.set_value(conn, "integration.gitlab.username", username)
    conn.commit()


def _test_anthropic():
    import anthropic

    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key,
        base_url=settings.anthropic_base_url or None,
        timeout=_CONNECTION_TEST_TIMEOUT_SECONDS,
        max_retries=0,
    )
    client.models.list(limit=1)


def _test_gitlab():
    from . import gitlab_client

    return gitlab_client.current_user()


def _test_clickup():
    from . import clickup_client

    # A settings check must verify the currently supplied credential, not a
    # user id cached for the token that was configured before rotation.
    clickup_client._user_id_cache = None
    clickup_client.current_user_id()


def _test_google_calendar():
    from . import gcal_client

    gcal_client._service(
        timeout_seconds=_CONNECTION_TEST_TIMEOUT_SECONDS
    ).calendarList().list(maxResults=1).execute()


def _test_flock():
    from . import flock_client

    if not flock_client.configured():
        raise RuntimeError("Flock profile is not connected")


def _run_bounded(source: str, test, *, timeout_seconds: float | None = None):
    """Run one synchronous test per source behind a hard Watson deadline.

    A timed-out daemon remains registered until its transport actually exits;
    callers cannot accumulate abandoned workers for the same connector.
    """
    if timeout_seconds is None:
        timeout_seconds = _CONNECTION_TEST_TIMEOUT_SECONDS
    result_queue = queue.Queue(maxsize=1)
    run_record = None

    def run():
        try:
            result_queue.put((True, test()))
        except Exception as error:
            result_queue.put((False, error))
        finally:
            with _connection_test_lock:
                if _connection_test_runs.get(source) is run_record:
                    _connection_test_runs.pop(source, None)

    worker = threading.Thread(
        target=run, name=f"settings-connection-test-{source}", daemon=True
    )
    run_record = _ConnectionTestRun(worker=worker, result_queue=result_queue)
    with _connection_test_lock:
        existing = _connection_test_runs.get(source)
        if existing is not None and existing.worker.is_alive():
            raise ConnectionTestBusy("connection test already running")
        _connection_test_runs[source] = run_record
        try:
            worker.start()
        except Exception:
            _connection_test_runs.pop(source, None)
            raise
    worker.join(max(0.0, timeout_seconds))
    if worker.is_alive():
        raise TimeoutError("connection test timed out")
    succeeded, result = result_queue.get_nowait()
    if succeeded:
        return result
    raise result


def test_integration(conn, source: str) -> dict:
    testers = {
        "anthropic": _test_anthropic,
        "gitlab": _test_gitlab,
        "clickup": _test_clickup,
        "google-calendar": _test_google_calendar,
        "flock": _test_flock,
    }
    try:
        tester = testers[source]
    except KeyError:
        raise ValueError("unsupported integration source") from None
    apply_runtime_settings(conn)
    try:
        result = _run_bounded(source, tester)
        if source == "gitlab":
            _upsert_self_gitlab_identity(conn, result)
        health = {"status": "connected"}
    except ConnectionTestBusy:
        raise
    except Exception:
        # Deliberately omit exception text: third-party libraries routinely
        # include request headers, tokens, or client JSON in their errors.
        health = {"status": "failed", "error": "Connection test failed."}
    app_settings.set_value(conn, f"integration.{source}.health", health)
    apply_runtime_settings(conn)
    return health


def _identity_dict(row) -> dict:
    return {
        "id": row["id"],
        "source": row["source"],
        "external_id": row["external_id"],
        "display_value": row["display_value"] or "",
    }


def _normalize_email_domain(value: str) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if _EMAIL_DOMAIN_RE.fullmatch(candidate) else ""


def _legacy_people_email_domain(conn) -> str:
    """Infer the pre-setting domain so existing installations keep working."""
    counts: dict[str, int] = {}
    rows = conn.execute(
        "SELECT external_id FROM person_identities "
        "WHERE source IN ('clickup', 'google-calendar', 'flock')"
    ).fetchall()
    for row in rows:
        external_id = str(row["external_id"] or "").strip()
        if "@" not in external_id:
            continue
        domain = _normalize_email_domain(external_id.rsplit("@", 1)[1])
        if domain:
            counts[domain] = counts.get(domain, 0) + 1
    return sorted(counts, key=lambda domain: (-counts[domain], domain))[0] if counts else ""


def _people_email_domain(conn, fallback: Settings | None = None) -> str:
    configured = _normalize_email_domain(
        app_settings.get(conn, "profile.email_domain", "")
    )
    if configured:
        return configured
    fallback = fallback or _fresh_environment()
    environment_value = _normalize_email_domain(fallback.people_email_domain)
    return environment_value or _legacy_people_email_domain(conn) or _DEFAULT_PEOPLE_EMAIL_DOMAIN


def _person_view(conn, row) -> dict:
    result = person_dict(row)
    identities = [
        _identity_dict(identity)
        for identity in conn.execute(
            "SELECT * FROM person_identities WHERE person_id=? ORDER BY source, id",
            (row["id"],),
        ).fetchall()
    ]
    result["identities"] = identities
    gitlab_identity = next(
        (identity["external_id"] for identity in identities if identity["source"] == "gitlab"),
        "",
    )
    email_suffix = f"@{_people_email_domain(conn)}"
    email_identity = next(
        (
            identity["external_id"][: -len(email_suffix)]
            for identity in identities
            if identity["external_id"].casefold().endswith(email_suffix)
        ),
        "",
    )
    result["identifier"] = gitlab_identity or email_identity
    return result


def list_people(conn) -> list[dict]:
    return [
        _person_view(conn, row)
        for row in conn.execute(
            "SELECT * FROM people WHERE is_self=1 OR is_tracked=1 "
            "ORDER BY is_self DESC, lane_position, id"
        ).fetchall()
    ]


def _lane_position(conn, tracked: bool) -> int:
    if not tracked:
        return 0
    return conn.execute(
        "SELECT COALESCE(MAX(lane_position), 0) + 1 AS position "
        "FROM people WHERE is_self=0 AND is_tracked=1"
    ).fetchone()["position"]


def _generated_identities(conn, identifier: str) -> tuple[tuple[str, str, str], ...]:
    email = f"{identifier}@{_people_email_domain(conn)}"
    return (
        ("gitlab", identifier, identifier),
        ("clickup", email, email),
        ("google-calendar", email, email),
        ("flock", email, email),
    )


def _replace_generated_email_identities(
    conn, person_id: int, identifier: str
) -> None:
    conn.execute(
        "DELETE FROM person_identities "
        "WHERE person_id=? AND source IN ('clickup', 'google-calendar', 'flock')",
        (person_id,),
    )
    for source, external_id, display_value in _generated_identities(conn, identifier):
        if source == "gitlab":
            continue
        conn.execute(
            "INSERT INTO person_identities "
            "(person_id, source, external_id, display_value) VALUES (?, ?, ?, ?)",
            (person_id, source, external_id, display_value),
        )


def _regenerate_tracked_email_identities(conn) -> None:
    rows = conn.execute(
        "SELECT p.id AS person_id, pi.external_id AS identifier "
        "FROM people p JOIN person_identities pi ON pi.person_id=p.id "
        "WHERE p.is_self=0 AND p.is_tracked=1 AND pi.source='gitlab' "
        "AND pi.id=(SELECT MIN(first_pi.id) FROM person_identities first_pi "
        "WHERE first_pi.person_id=p.id AND first_pi.source='gitlab') "
        "ORDER BY p.id"
    ).fetchall()
    for row in rows:
        _replace_generated_email_identities(
            conn, row["person_id"], row["identifier"]
        )


def _replace_generated_identities(conn, person_id: int, identifier: str) -> None:
    conn.execute("DELETE FROM person_identities WHERE person_id=?", (person_id,))
    for source, external_id, display_value in _generated_identities(conn, identifier):
        conn.execute(
            "INSERT INTO person_identities "
            "(person_id, source, external_id, display_value) VALUES (?, ?, ?, ?)",
            (person_id, source, external_id, display_value),
        )


def _validate_person_name(display_name: str) -> str:
    display_name = display_name.strip()
    if not display_name:
        raise ValueError("display_name must not be blank")
    return display_name


def create_person(conn, payload) -> dict:
    display_name = _validate_person_name(payload.display_name)
    identifier = payload.identifier
    timestamp = now_iso()
    try:
        conn.execute("BEGIN")
        matching_person_ids = {
            row["person_id"]
            for source, external_id, _display_value in _generated_identities(conn, identifier)
            for row in conn.execute(
                "SELECT person_id FROM person_identities "
                "WHERE source=? AND external_id=?",
                (source, external_id),
            ).fetchall()
        }
        if len(matching_person_ids) > 1:
            raise ValueError("identities are assigned to different people")

        person_id = next(iter(matching_person_ids), None)
        if person_id is not None:
            existing = conn.execute(
                "SELECT * FROM people WHERE id=?", (person_id,)
            ).fetchone()
            if existing["is_self"] or existing["is_tracked"]:
                raise ValueError("identity is already assigned to another person")
            conn.execute(
                "UPDATE people SET display_name=?, is_tracked=?, lane_position=?, "
                "updated_at=? WHERE id=?",
                (
                    display_name,
                    1,
                    _lane_position(conn, True),
                    timestamp,
                    person_id,
                ),
            )
        else:
            cursor = conn.execute(
                "INSERT INTO people "
                "(display_name, is_self, is_tracked, lane_position, created_at, updated_at) "
                "VALUES (?, 0, ?, ?, ?, ?)",
                (
                    display_name,
                    1,
                    _lane_position(conn, True),
                    timestamp,
                    timestamp,
                ),
            )
            person_id = cursor.lastrowid
        _replace_generated_identities(conn, person_id, identifier)
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError("identity is already assigned to another person") from None
    except ValueError:
        conn.rollback()
        raise
    row = conn.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
    return _person_view(conn, row)


def update_person(conn, person_id: int, payload) -> dict:
    row = conn.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
    if row is None:
        raise LookupError("person not found")
    if row["is_self"]:
        raise ValueError("edit the self person through profile settings")
    display_name = (
        payload.display_name.strip()
        if payload.display_name is not None
        else row["display_name"]
    )
    display_name = _validate_person_name(display_name)
    try:
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE people SET display_name=?, updated_at=? WHERE id=?",
            (display_name, now_iso(), person_id),
        )
        if payload.identifier is not None:
            _replace_generated_identities(conn, person_id, payload.identifier)
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError("identity is already assigned to another person") from None
    return _person_view(
        conn, conn.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
    )


def delete_person(conn, person_id: int) -> dict:
    row = conn.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
    if row is None:
        raise LookupError("person not found")
    if row["is_self"]:
        raise ValueError("the self person cannot be deleted")
    linked = conn.execute(
        "SELECT 1 FROM work_items WHERE owner_person_id=? LIMIT 1", (person_id,)
    ).fetchone()
    if linked:
        conn.execute(
            "UPDATE people SET is_tracked=0, lane_position=0, updated_at=? WHERE id=?",
            (now_iso(), person_id),
        )
        conn.commit()
        return {"deleted": False, "demoted": True}
    conn.execute("DELETE FROM people WHERE id=?", (person_id,))
    conn.commit()
    return {"deleted": True, "demoted": False}


def update_data_settings(conn, backup_dir: str) -> dict:
    from ..db import db_path

    try:
        target = Path(backup_dir.strip()).expanduser().resolve()
        live_database = db_path().resolve()
        if target.exists() and not target.is_dir():
            raise ValueError("backup path must be a directory")
    except (OSError, RuntimeError):
        raise ValueError("backup path could not be resolved") from None
    if target in (live_database, live_database.parent):
        raise ValueError(
            "backup directory must differ from the live data directory and database"
        )
    app_settings.set_value(conn, "data.backup_dir", str(target))
    return {"backup_dir": str(target)}
