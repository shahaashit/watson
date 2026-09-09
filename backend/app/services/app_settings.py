"""Persistent, non-secret runtime application settings."""

import json

from ..models import now_iso


def get(conn, key: str, default=None):
    row = conn.execute(
        "SELECT value_json FROM app_settings WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return default
    return json.loads(row["value_json"])


def set_value(conn, key: str, value, *, commit: bool = True) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value_json, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET"
        " value_json = excluded.value_json, updated_at = excluded.updated_at",
        (key, json.dumps(value), now_iso()),
    )
    if commit:
        conn.commit()


def delete(conn, key: str, *, commit: bool = True) -> None:
    conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
    if commit:
        conn.commit()


def snapshot(conn, prefix: str = "") -> dict:
    rows = conn.execute(
        "SELECT key, value_json FROM app_settings ORDER BY key"
    ).fetchall()
    return {
        row["key"]: json.loads(row["value_json"])
        for row in rows
        if row["key"].startswith(prefix)
    }


def effective_nonsecret(key: str, env_fallback):
    from ..db import connect

    conn = connect()
    try:
        return get(conn, key, env_fallback)
    finally:
        conn.close()


def integration_disabled(source: str) -> bool:
    """Fail closed when a connector cannot read its persistent tombstone."""
    try:
        return bool(effective_nonsecret(f"integration.{source}.disabled", False))
    except Exception:
        return True
