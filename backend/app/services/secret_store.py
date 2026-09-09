"""macOS Keychain storage for integration credentials.

Secrets are intentionally kept outside Watson's database and settings files.
Errors identify only the logical setting name so credential material cannot be
accidentally surfaced to an API response or log.
"""

import subprocess


SECURITY = "/usr/bin/security"
SERVICE = "com.watson.local"
_NOT_FOUND_RETURN_CODE = 44


class SecretStoreError(RuntimeError):
    """A Keychain operation failed without disclosing credential material."""


def _run(command: list[str], name: str, *, missing_is_expected: bool = False):
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except Exception:
        raise SecretStoreError(f"Unable to access Keychain secret '{name}'.") from None

    if result.returncode == 0:
        return result
    if missing_is_expected and result.returncode == _NOT_FOUND_RETURN_CODE:
        return None
    raise SecretStoreError(f"Unable to access Keychain secret '{name}'.")


def _find_secret(name: str):
    return _run(
        [SECURITY, "find-generic-password", "-s", SERVICE, "-a", name, "-w"],
        name,
        missing_is_expected=True,
    )


def get_secret(name: str) -> str:
    """Return a stored secret, or an empty string when its Keychain item is absent."""
    result = _find_secret(name)
    if result is None:
        return ""
    return result.stdout.rstrip("\r\n") if isinstance(result.stdout, str) else ""


def set_secret(name: str, value: str) -> None:
    """Create or update a Keychain item for a logical Watson secret name."""
    _run(
        [
            SECURITY,
            "add-generic-password",
            "-U",
            "-s",
            SERVICE,
            "-a",
            name,
            "-w",
            value,
        ],
        name,
    )


def delete_secret(name: str) -> bool:
    """Delete a stored secret, returning whether an item was actually removed."""
    result = _run(
        [SECURITY, "delete-generic-password", "-s", SERVICE, "-a", name],
        name,
        missing_is_expected=True,
    )
    return result is not None


def has_secret(name: str) -> bool:
    """Report whether the logical secret has a Keychain item, including empty values."""
    return _find_secret(name) is not None


def effective_secret(name: str, env_fallback: str = "") -> str:
    """Prefer a Keychain value while retaining read-only environment compatibility."""
    result = _find_secret(name)
    if result is None:
        return env_fallback
    return result.stdout.rstrip("\r\n") if isinstance(result.stdout, str) else ""
