"""Static contracts for the shareable macOS packaging scripts.

The installer deliberately is not executed in tests: it can touch launchd.
These contracts pin its fail-closed safety controls and the scratch migration
fixture's preservation checks.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_migration_verifier_preserves_seeded_gitlab_mr_across_both_runs():
    source = (ROOT / "scripts" / "verify-migration.sh").read_text()

    assert "Legacy MR" in source
    assert "SELECT COUNT(*) FROM gitlab_mrs_cache" in source
    assert "SELECT project, title, state, url FROM gitlab_mrs_cache" in source


def test_launch_agent_isolates_environment_and_has_fail_closed_rollback_guards():
    source = (ROOT / "scripts" / "install-launch-agent.sh").read_text()

    assert "<string>/usr/bin/env</string>" in source
    assert "<string>-i</string>" in source
    assert "HOME=$(xml \"$HOME\")" in source
    assert "PATH=/usr/bin:/bin" in source
    assert "assert_port_ownership" in source
    assert "restore_previous_service" in source
    assert "health_is_watson" in source
    assert "lsof" in source


def test_launch_agent_accepts_launchctl_pid_with_or_without_semicolon():
    source = (ROOT / "scripts" / "install-launch-agent.sh").read_text()

    # macOS launchctl output differs by release: both `pid = 123` and
    # `pid = 123;` are valid. The ownership guard must recognize either.
    assert "([0-9]+);?[[:space:]]*$" in source
