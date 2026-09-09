import subprocess
from types import SimpleNamespace

import pytest

from app.services import secret_store


@pytest.fixture
def security_run(monkeypatch):
    """Mock the macOS Keychain process boundary for every test."""
    calls = []
    responses = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(secret_store.subprocess, "run", run)
    return calls, responses


def test_set_uses_update_or_insert_with_captured_text_output(security_run):
    """Catches accidentally creating duplicate items or leaking process output."""
    calls, responses = security_run
    responses.append(SimpleNamespace(returncode=0, stdout="", stderr=""))

    secret_store.set_secret("gitlab.token", "super-secret")

    assert calls == [(
        [
            "/usr/bin/security", "add-generic-password", "-U", "-s",
            "com.watson.local", "-a", "gitlab.token", "-w", "super-secret",
        ],
        {"capture_output": True, "text": True},
    )]


def test_effective_secret_prefers_keychain_over_environment(security_run):
    """Catches runtime configuration falling back before consulting Keychain."""
    _, responses = security_run
    responses.append(SimpleNamespace(returncode=0, stdout="keychain-value", stderr=""))

    assert secret_store.effective_secret("gitlab.token", "env-value") == "keychain-value"


def test_get_reads_the_logical_account_from_watson_service(security_run):
    """Catches reading a differently named Keychain service or account."""
    calls, responses = security_run
    responses.append(SimpleNamespace(returncode=0, stdout="stored-token", stderr=""))

    assert secret_store.get_secret("gitlab.token") == "stored-token"
    assert calls == [(
        [
            "/usr/bin/security", "find-generic-password", "-s", "com.watson.local",
            "-a", "gitlab.token", "-w",
        ],
        {"capture_output": True, "text": True},
    )]


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("super-secret-value command-arg stdout stderr"),
        subprocess.CalledProcessError(
            1,
            ["/usr/bin/security", "-w", "super-secret-value"],
            output="super-secret-stdout",
            stderr="super-secret-stderr",
        ),
        subprocess.TimeoutExpired(
            ["/usr/bin/security", "-w", "super-secret-value"],
            1,
            output="super-secret-stdout",
            stderr="super-secret-stderr",
        ),
    ],
)
def test_subprocess_exceptions_are_redacted_from_secret_store_errors(security_run, failure):
    """Catches leaking process arguments or diagnostics through exception context."""
    _, responses = security_run
    responses.append(failure)

    with pytest.raises(secret_store.SecretStoreError) as raised:
        secret_store.set_secret("gitlab.token", "super-secret-value")

    assert str(raised.value) == "Unable to access Keychain secret 'gitlab.token'."
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


@pytest.mark.parametrize("stdout", ["stored-token\n", "stored-token\r\n"])
def test_get_removes_only_security_cli_line_terminator(security_run, stdout):
    """Catches returning the terminal newline security appends to a stored value."""
    _, responses = security_run
    responses.append(SimpleNamespace(returncode=0, stdout=stdout, stderr=""))

    assert secret_store.get_secret("gitlab.token") == "stored-token"


def test_get_returns_empty_only_when_keychain_item_is_not_found(security_run):
    """Catches treating every Keychain failure as a missing secret."""
    _, responses = security_run
    responses.append(SimpleNamespace(returncode=44, stdout="", stderr="not found"))

    assert secret_store.get_secret("gitlab.token") == ""


def test_keychain_errors_redact_credential_material(security_run):
    """Catches errors that expose secret values or Keychain process diagnostics."""
    _, responses = security_run
    responses.append(SimpleNamespace(
        returncode=1,
        stdout="super-secret-output",
        stderr="super-secret-diagnostic",
    ))

    with pytest.raises(RuntimeError) as raised:
        secret_store.set_secret("gitlab.token", "super-secret-value")

    message = str(raised.value)
    assert "gitlab.token" in message
    assert "super-secret-value" not in message
    assert "super-secret-output" not in message
    assert "super-secret-diagnostic" not in message


def test_delete_returns_false_only_when_keychain_item_is_not_found(security_run):
    """Catches claiming a deleted secret when no matching Keychain item exists."""
    calls, responses = security_run
    responses.append(SimpleNamespace(returncode=44, stdout="", stderr="not found"))

    assert secret_store.delete_secret("gitlab.token") is False
    assert calls == [(
        [
            "/usr/bin/security", "delete-generic-password", "-s", "com.watson.local",
            "-a", "gitlab.token",
        ],
        {"capture_output": True, "text": True},
    )]


def test_has_secret_reports_an_existing_empty_keychain_value(security_run):
    """Catches confusing a present-but-empty value with an absent Keychain item."""
    _, responses = security_run
    responses.append(SimpleNamespace(returncode=0, stdout="", stderr=""))

    assert secret_store.has_secret("gitlab.token") is True


def test_effective_secret_falls_back_when_keychain_item_is_not_found(security_run):
    """Catches environment compatibility being lost when no stored secret exists."""
    _, responses = security_run
    responses.append(SimpleNamespace(returncode=44, stdout="", stderr="not found"))

    assert secret_store.effective_secret("gitlab.token", "env-value") == "env-value"


def test_effective_secret_keeps_a_present_empty_keychain_value(security_run):
    """Catches replacing an intentionally empty stored secret with an environment value."""
    calls, responses = security_run
    responses.append(SimpleNamespace(returncode=0, stdout="", stderr=""))

    assert secret_store.effective_secret("gitlab.token", "env-value") == ""
    assert len(calls) == 1
