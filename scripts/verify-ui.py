#!/usr/bin/env python3
"""Scratch-only browser verification for the built Watson Work OS.

It owns one temporary data directory and one uvicorn child on port 8011. It
never calls a live connector, targets the launchd server, or terminates a
process it did not start.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
PORT = 8011
BASE_URL = f"http://127.0.0.1:{PORT}"
TMP_ROOT = Path(tempfile.gettempdir()).resolve()


def require_built_ui() -> None:
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        raise RuntimeError("frontend/dist is missing; run npm run build first")


def assert_port_available() -> None:
    """Fail before launching if another local process owns the reserved port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", PORT))
        except OSError as exc:
            raise RuntimeError(
                f"port {PORT} is already in use; refusing to disturb another process"
            ) from exc


def safe_cleanup(path: Path) -> None:
    resolved = path.resolve()
    if (
        resolved.parent == TMP_ROOT
        and resolved.name.startswith("watson-ui-")
        and resolved.is_dir()
    ):
        shutil.rmtree(resolved)


def seed_scratch(data_dir: Path) -> dict[str, int]:
    # ``app.config.settings`` is module-level, so its environment must be in
    # place before this import. Refuse to seed if an import resolves anywhere
    # except this unique mktemp directory.
    sys.path.insert(0, str(BACKEND))
    from app import db
    from app.models import now_iso
    from app.services import app_settings, review_groups, user_meta, work_items

    resolved_data = data_dir.resolve()
    resolved_db = db.db_path().resolve()
    if resolved_db.parent != resolved_data:
        raise RuntimeError(
            f"scratch safety check failed: resolved database is outside scratch ({resolved_db})"
        )
    db.init_db()
    conn = db.connect()
    try:
        stamp = now_iso()
        app_settings.set_value(conn, "onboarding.completed", True)
        app_settings.set_value(conn, "onboarding.step", 4)
        self_id = work_items.ensure_self_person(conn, "Sample User")
        alice_id = conn.execute(
            "INSERT INTO people (display_name, is_self, is_tracked, lane_position, created_at, updated_at) "
            "VALUES ('Alex Chen', 0, 1, 1, ?, ?)",
            (stamp, stamp),
        ).lastrowid
        bob_id = conn.execute(
            "INSERT INTO people (display_name, is_self, is_tracked, lane_position, created_at, updated_at) "
            "VALUES ('Blair Lee', 0, 1, 2, ?, ?)",
            (stamp, stamp),
        ).lastrowid

        mine = work_items.create_work_item(
            conn, title="Prepare the weekly delivery note", description="A local card remains useful without an integration.",
            state="today", owner_person_id=self_id,
        )
        work_items.create_work_item(
            conn, title="Review the release checklist", state="next", owner_person_id=self_id,
        )
        historical_done = work_items.create_work_item(
            conn, title="Historical completed sample", state="done", owner_person_id=self_id,
        )
        conn.execute(
            "UPDATE work_items SET completed_at=? WHERE id=?",
            ((datetime.now() - timedelta(days=3)).isoformat(timespec="seconds"), historical_done["id"]),
        )
        for index in range(16):
            work_items.create_work_item(
                conn, title=f"Alex priority {index + 1}", state="next", owner_person_id=alice_id,
            )
        work_items.create_work_item(conn, title="Blair’s MR context", state="waiting", owner_person_id=bob_id)
        work_items.create_work_item(conn, title="Recognized external owner", state="next", owner_display="Casey Lee")
        work_items.create_work_item(conn, title="Owner has not been resolved", state="next")
        work_items.add_activity(conn, mine["id"], activity_type="decision", body="Keep this plan local until it is ready to share.")
        work_items.add_work_link(
            conn, mine["id"], source_type="url", external_id="sample-reference", url="https://example.com/reference", label="Sample reference",
        )
        clickup_url = "https://app.clickup.com/t/86d3sample"
        work_items.add_work_link(
            conn, mine["id"], source_type="clickup", external_id="86d3sample", url=clickup_url, label="Opaque ClickUp label",
        )

        def seed_review_mr(mr_id: str, *, title: str, project: str) -> None:
            conn.execute(
                "INSERT INTO gitlab_mrs_cache "
                "(mr_id, project, title, state, url, role, author, author_username, "
                "source_branch, description, updated_at, synced_at) "
                "VALUES (?, ?, ?, 'opened', ?, 'reviewer', 'Alex Chen', 'alex.chen', "
                "'', 'Scratch-only review context', ?, ?)",
                (
                    mr_id,
                    project,
                    title,
                    f"https://gitlab.example/{project}/-/merge_requests/{mr_id.split('!')[1]}",
                    stamp,
                    stamp,
                ),
            )

        seed_review_mr("101!10", title="Semantic rollout API", project="watson/backend")
        seed_review_mr("202!20", title="Semantic rollout UI", project="watson/frontend")
        grouped_review = work_items.create_work_item(
            conn,
            title="Review - Semantic rollout (Alex Chen)",
            description="One local card for two high-confidence related review MRs.",
            state="next",
            owner_person_id=self_id,
            origin="discovery",
        )
        for mr_id in ("101!10", "202!20"):
            work_items.add_work_link(
                conn,
                grouped_review["id"],
                source_type="gitlab_mr",
                external_id=mr_id,
            )
        grouped = review_groups.upsert_group(
            conn,
            fingerprint="ai:101!10+202!20",
            author_username="alex.chen",
            title="Semantic rollout",
            description="Scratch-only review context",
            provenance="ai",
            confidence=0.93,
            mr_ids=["101!10", "202!20"],
        )
        review_groups.mark_created(
            conn,
            grouped["id"],
            "CU-GROUPED-REVIEW",
            work_item_id=grouped_review["id"],
        )

        ambiguous_id = conn.execute(
            "INSERT INTO pending_actions "
            "(kind, target_id, payload_json, status, created_at) "
            "VALUES ('group_review_mrs', 'ai:303!30+404!40', ?, 'pending', ?)",
            (
                json.dumps({
                    "fingerprint": "ai:303!30+404!40",
                    "suggested_title": "Ambiguous rollout",
                    "reasoning": "The cached context overlaps but is not decisive.",
                    "member_mr_ids": ["303!30", "404!40"],
                    "confidence": 0.78,
                }),
                stamp,
            ),
        ).lastrowid
        failed_group = review_groups.upsert_group(
            conn,
            fingerprint="singleton:505!50",
            author_username="failed.author",
            title="Definite failure review",
            description="",
            provenance="singleton",
            confidence=1.0,
            mr_ids=["505!50"],
        )
        conn.execute(
            "UPDATE review_groups SET creation_state='failed', last_error=? WHERE id=?",
            ("ClickUp review task creation failed", failed_group["id"]),
        )
        failed_id = conn.execute(
            "INSERT INTO pending_actions "
            "(kind, payload_json, status, error, created_at) "
            "VALUES ('clickup_create_task', ?, 'failed', ?, ?)",
            (
                json.dumps({
                    "auto_proposed": True,
                    "review_group_id": failed_group["id"],
                    "draft": {"name": "Review - Definite failure review"},
                    "member_mr_ids": ["505!50"],
                }),
                "ClickUp review task creation failed",
                stamp,
            ),
        ).lastrowid
        uncertain_group = review_groups.upsert_group(
            conn,
            fingerprint="singleton:606!60",
            author_username="uncertain.author",
            title="Uncertain review result",
            description="",
            provenance="singleton",
            confidence=1.0,
            mr_ids=["606!60"],
        )
        review_groups.mark_uncertain(
            conn,
            uncertain_group["id"],
            "ClickUp review task creation failed",
        )
        duplicate_id = conn.execute(
            "INSERT INTO pending_actions "
            "(kind, target_id, payload_json, status, created_at) "
            "VALUES ('clickup_close_task', 'CU-DUPLICATE', ?, 'pending', ?)",
            (json.dumps({"reason": "Duplicate after merge approval"}), stamp),
        ).lastrowid

        capture_id = conn.execute(
            "INSERT INTO captures (raw_text, created_at, classified_at, classification_json, work_item_id) VALUES (?, ?, ?, ?, ?)",
            ("A draft external update is waiting for an explicit decision.", stamp, stamp, "{}", mine["id"]),
        ).lastrowid
        action_id = conn.execute(
            "INSERT INTO pending_actions (capture_id, kind, target_id, payload_json, match_confidence, status, created_at) "
            "VALUES (?, 'clickup_comment', 'sample-clickup', ?, 0.9, 'pending', ?)",
            (capture_id, json.dumps({"draft": "Sample external comment"}), stamp),
        ).lastrowid
        tomorrow = datetime.now() + timedelta(hours=1)
        conn.execute(
            "INSERT INTO gcal_events_cache (event_id, calendar_id, title, description, location, start_at, end_at, all_day, organizer, attendees, my_response, html_link, synced_at) "
            "VALUES ('sample-calendar-event', 'primary', 'Design sync', '', '', ?, ?, 0, '', '[]', 'accepted', 'https://calendar.example/event', ?)",
            (tomorrow.isoformat(timespec="seconds"), (tomorrow + timedelta(minutes=30)).isoformat(timespec="seconds"), stamp),
        )
        user_meta.set_integration_health(conn, "clickup", {
            "status": "degraded",
            "last_success_at": stamp,
            "message": "Cached work is available. Retry from Settings.",
        })
        conn.commit()
        return {
            "mine": mine["id"],
            "action": action_id,
            "grouped_review": grouped_review["id"],
            "ambiguous_action": ambiguous_id,
            "failed_action": failed_id,
            "uncertain_group": uncertain_group["id"],
            "duplicate_action": duplicate_id,
        }
    finally:
        conn.close()


def wait_for_server(process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("scratch uvicorn exited before becoming healthy")
        try:
            with urllib.request.urlopen(f"{BASE_URL}/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("scratch uvicorn did not become healthy within 20 seconds")


def assert_no_horizontal_overflow(page, label: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    assert not overflow, f"{label}: document has horizontal overflow"


def run_browser_checks(ids: dict[str, int]) -> None:
    from playwright.sync_api import sync_playwright

    routes = ["/my-work", "/team", f"/work/{ids['mine']}", "/settings", "/log"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for width in (1440, 390):
                page = browser.new_page(viewport={"width": width, "height": 900})
                write_requests = []
                page.on(
                    "request",
                    lambda request: write_requests.append((request.method, request.url))
                    if request.method != "GET"
                    else None,
                )
                for route in routes:
                    page.goto(f"{BASE_URL}{route}", wait_until="networkidle")
                    page.wait_for_selector(".nav")
                    assert_no_horizontal_overflow(page, f"{route} at {width}px")
                    assert page.evaluate(
                        "getComputedStyle(document.documentElement).fontSize"
                    ) == "18px", "Watson should use the approved larger base type"
                assert page.locator(".nav-btn").all_inner_texts() == [
                    "My Work", "Team", "Log"
                ], "review automation must not add a navigation tab"

                page.goto(f"{BASE_URL}/my-work", wait_until="networkidle")
                page.wait_for_selector(".work-card", timeout=5000)
                assert page.locator(".work-card").count() >= 2, "My Work should contain real local work"
                assert page.get_by_role(
                    "button", name="Open Review - Semantic rollout (Alex Chen)"
                ).count() == 1, "two related review MRs should render as one grouped card"
                assert page.get_by_role("heading", name="Priority").count() == 1
                assert page.locator(".work-card-state").count() == 0
                my_title_style = page.locator(".work-card-title").first.evaluate(
                    "node => ({ clamp: getComputedStyle(node).webkitLineClamp, whiteSpace: getComputedStyle(node).whiteSpace })"
                )
                assert my_title_style == {"clamp": "2", "whiteSpace": "normal"}
                assert "Historical completed sample" not in page.locator("body").inner_text()
                assert page.locator(".schedule-strip").count() == 1, "My Work should keep a compact schedule"
                suggestions = page.get_by_role("region", name="Watson suggestions")
                assert suggestions.count() == 1
                for label in (
                    "Merge",
                    "Keep separate",
                    "Retry",
                    "Close task",
                ):
                    assert suggestions.get_by_text(label, exact=True).count() == 1, (
                        f"Watson suggests should expose {label!r} exactly once"
                    )
                assert suggestions.get_by_text(
                    "Check ClickUp before resolving. Watson will not retry an uncertain external write.",
                    exact=True,
                ).count() == 1
                page.locator(".nav-add-work").click()
                page.get_by_role("button", name="Import link").click()
                assert page.get_by_label("GitLab MR or ClickUp task URL").is_editable(), "manual external import must be reachable"
                page.locator(".integration-health-toggle").click()
                assert page.get_by_text("Cached work is available. Retry from Settings.").count() == 1

                page.goto(f"{BASE_URL}/team", wait_until="networkidle")
                for lane_name in ("Alex Chen", "Blair Lee", "Others", "Unassigned"):
                    assert page.get_by_role("heading", name=lane_name).count() == 1
                scrolls = page.locator(".person-lane-scroll")
                assert scrolls.count() >= 4
                assert all(
                    scrolls.nth(index).evaluate("node => getComputedStyle(node).overflowY") == "auto"
                    for index in range(scrolls.count())
                )
                assert any(
                    scrolls.nth(index).evaluate("node => node.scrollHeight > node.clientHeight")
                    for index in range(scrolls.count())
                ), "At least one Team lane must visibly overflow internally"
                assert "+N" not in page.locator("body").inner_text()
                assert page.locator(".person-lane-state").count() == 0
                team_title_style = page.locator(".person-lane-card .work-card-title").first.evaluate(
                    "node => ({ clamp: getComputedStyle(node).webkitLineClamp, whiteSpace: getComputedStyle(node).whiteSpace })"
                )
                assert team_title_style == {"clamp": "3", "whiteSpace": "normal"}

                page.goto(f"{BASE_URL}/work/{ids['mine']}", wait_until="networkidle")
                page.get_by_role("heading", name="Prepare the weekly delivery note").wait_for()
                page.reload(wait_until="networkidle")
                page.get_by_role("heading", name="Prepare the weekly delivery note").wait_for()
                assert page.get_by_role("heading", name="Capture to this work").count() == 1, "work capture must be reachable"
                assert page.get_by_label("Capture for this work").is_editable()
                assert page.get_by_role("button", name="Approve").count() == 1
                assert page.get_by_role("button", name="Reject").count() == 1
                assert page.get_by_label("Local state").count() == 0
                assert page.get_by_text("Local priority", exact=True).count() == 0
                clickup_url = "https://app.clickup.com/t/86d3sample"
                clickup_link = page.locator(f'a[href="{clickup_url}"]')
                assert clickup_link.count() == 1
                assert clickup_link.inner_text().strip().startswith(clickup_url)
                page.get_by_role("button", name="Back to My Work").click()
                page.wait_for_url(f"{BASE_URL}/my-work")

                page.goto(f"{BASE_URL}/settings", wait_until="networkidle")
                page.get_by_role("heading", name="Watson stays yours to edit.").wait_for()
                assert page.get_by_label("Display name").is_editable()
                create_reviews = page.get_by_role(
                    "checkbox", name="Create ClickUp review tasks automatically"
                )
                group_reviews = page.get_by_role(
                    "checkbox", name="Group related review MRs using AI"
                )
                if width == 1440:
                    assert create_reviews.is_checked(), "missing auto-create preference should default on"
                    assert group_reviews.is_checked(), "missing AI grouping preference should default on"
                    create_reviews.uncheck()
                    group_reviews.uncheck()
                    page.get_by_role("button", name="Save profile").click()
                    page.get_by_text("Profile saved locally.").wait_for()
                    page.reload(wait_until="networkidle")
                assert not page.get_by_role(
                    "checkbox", name="Create ClickUp review tasks automatically"
                ).is_checked(), "auto-create preference should persist off"
                assert not page.get_by_role(
                    "checkbox", name="Group related review MRs using AI"
                ).is_checked(), "AI grouping preference should persist off"
                if width == 390:
                    page.get_by_role(
                        "checkbox", name="Create ClickUp review tasks automatically"
                    ).check()
                    page.get_by_role(
                        "checkbox", name="Group related review MRs using AI"
                    ).check()
                    page.get_by_role("button", name="Save profile").click()
                    page.get_by_text("Profile saved locally.").wait_for()
                    page.reload(wait_until="networkidle")
                    assert page.get_by_role(
                        "checkbox", name="Create ClickUp review tasks automatically"
                    ).is_checked(), "auto-create preference should persist on"
                    assert page.get_by_role(
                        "checkbox", name="Group related review MRs using AI"
                    ).is_checked(), "AI grouping preference should persist on"
                assert all(
                    method == "PATCH" and url == f"{BASE_URL}/api/settings/profile"
                    for method, url in write_requests
                ), "browser verification must not invoke an external-write endpoint"
                page.close()
        finally:
            browser.close()


def assert_action_still_pending(data_dir: Path, action_id: int) -> None:
    import sqlite3

    conn = sqlite3.connect(data_dir / "watson.db")
    try:
        status = conn.execute("SELECT status FROM pending_actions WHERE id=?", (action_id,)).fetchone()[0]
        assert status == "pending", "browser verification must not execute an external action"
    finally:
        conn.close()


def assert_review_fixtures_unchanged(data_dir: Path, ids: dict[str, int]) -> None:
    import sqlite3

    conn = sqlite3.connect(data_dir / "watson.db")
    try:
        statuses = dict(
            conn.execute(
                "SELECT id, status FROM pending_actions WHERE id IN (?, ?, ?)",
                (
                    ids["ambiguous_action"],
                    ids["failed_action"],
                    ids["duplicate_action"],
                ),
            ).fetchall()
        )
        assert statuses == {
            ids["ambiguous_action"]: "pending",
            ids["failed_action"]: "failed",
            ids["duplicate_action"]: "pending",
        }, "review controls must remain untouched during browser verification"
        group = conn.execute(
            "SELECT creation_state FROM review_groups WHERE id=?",
            (ids["uncertain_group"],),
        ).fetchone()
        assert group == ("uncertain",)
        members = conn.execute(
            "SELECT mr_id FROM review_group_mrs WHERE group_id=("
            "SELECT id FROM review_groups WHERE work_item_id=?) ORDER BY mr_id",
            (ids["grouped_review"],),
        ).fetchall()
        assert members == [("101!10",), ("202!20",)]
    finally:
        conn.close()


def main() -> int:
    require_built_ui()
    assert_port_available()
    scratch = Path(tempfile.mkdtemp(prefix="watson-ui-"))
    process: subprocess.Popen[str] | None = None
    try:
        data_dir = scratch / "data"
        # Configure the module-level Settings object before importing any app
        # code to make the scratch boundary true for both seeding and serving.
        os.environ["WATSON_DATA_DIR"] = str(data_dir)
        os.environ["WATSON_BACKUP_DIR"] = str(scratch / "backup")
        os.environ["TESTING"] = "1"
        ids = seed_scratch(data_dir)
        environment = os.environ.copy()
        environment.update({
            "WATSON_DATA_DIR": str(data_dir),
            "WATSON_BACKUP_DIR": str(scratch / "backup"),
            "TESTING": "1",
            "PYTHONPATH": str(BACKEND),
            "ANTHROPIC_API_KEY": "",
            "CLICKUP_API_TOKEN": "",
            "GITLAB_TOKEN": "",
        })
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
            cwd=BACKEND, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
        )
        wait_for_server(process)
        run_browser_checks(ids)
        assert_action_still_pending(data_dir, ids["action"])
        assert_review_fixtures_unchanged(data_dir, ids)
        print("UI verification passed: review settings, grouped card, reconciliation controls, navigation, responsive overflow, and external-write gate")
        return 0
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        safe_cleanup(scratch)


if __name__ == "__main__":
    raise SystemExit(main())
