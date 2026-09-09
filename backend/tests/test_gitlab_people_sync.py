import json
from types import MappingProxyType

import pytest

from app.models import mr_dict
from app.services import app_settings, gitlab_client, mr_review_tracker


RUNTIME_CONFIG = MappingProxyType(
    {
        "base_url": "https://gitlab.example.com",
        "username": "alex",
        "token": "test-token",
    }
)


def _mr(
    project_id=41,
    iid=17,
    *,
    project="group/project",
    author="morgan.dev",
    assignees=("alex",),
    reviewers=("alex",),
):
    return {
        "project_id": project_id,
        "iid": iid,
        "references": {"full": f"{project}!{iid}"},
        "title": "Ship global MR ingestion",
        "state": "opened",
        "web_url": f"https://gitlab.example.com/{project}/-/merge_requests/{iid}",
        "author": {"name": "Morgan G", "username": author},
        "assignees": [
            {"name": username.title(), "username": username}
            for username in assignees
        ],
        "reviewers": [
            {"name": username.title(), "username": username}
            for username in reviewers
        ],
        "source_branch": "feature/global-ingestion_clickup_86abc1234",
        "description": "Keeps the work cache complete.",
        "labels": ["Ready for QA", "backend"],
        "created_at": "2026-08-20T09:10:11.000+05:30",
        "updated_at": "2026-08-27T10:11:12.000+05:30",
    }


class _Response:
    def __init__(self, body, *, next_page=""):
        self._body = body
        self.headers = {"X-Next-Page": next_page}
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def _add_identity(conn, name, username, *, tracked=True, source="gitlab"):
    conn.execute(
        "INSERT INTO people (display_name,is_tracked,created_at,updated_at)"
        " VALUES (?,?, '2026-08-27', '2026-08-27')",
        (name, int(tracked)),
    )
    person_id = conn.execute(
        "SELECT id FROM people WHERE display_name=?", (name,)
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO person_identities (person_id,source,external_id)"
        " VALUES (?,?,?)",
        (person_id, source, username),
    )
    conn.commit()


def test_tracked_gitlab_usernames_returns_only_explicit_tracked_identities(conn):
    _add_identity(conn, "Morgan", "morgan.dev")
    _add_identity(conn, "Untracked", "untracked.g", tracked=False)
    _add_identity(conn, "ClickUp only", "clickup-user", source="clickup")

    assert gitlab_client.tracked_gitlab_usernames(conn) == ["morgan.dev"]


def test_accessible_projects_are_paginated_and_normalized(monkeypatch):
    calls = []

    def get(url, *, headers, params, timeout):
        calls.append(dict(params))
        if params["page"] == 1:
            return _Response(
                [
                    {"id": 41, "name": "frontend-service", "path_with_namespace": "cm/frontend-service", "archived": False},
                    {"id": 99, "name": "archived", "path_with_namespace": "old/archived", "archived": True},
                ],
                next_page="2",
            )
        return _Response(
            [{"id": 52, "name": "backend-service", "path_with_namespace": "cm/go/backend-service", "archived": False}]
        )

    monkeypatch.setattr(gitlab_client.requests, "get", get)

    assert gitlab_client.list_accessible_projects(RUNTIME_CONFIG) == [
        {"id": 41, "name": "frontend-service", "path": "cm/frontend-service"},
        {"id": 52, "name": "backend-service", "path": "cm/go/backend-service"},
    ]
    assert calls == [
        {"membership": True, "simple": True, "archived": False, "order_by": "path", "sort": "asc", "per_page": 100, "page": 1},
        {"membership": True, "simple": True, "archived": False, "order_by": "path", "sort": "asc", "per_page": 100, "page": 2},
    ]


def test_accessible_project_is_resolved_by_stable_id(monkeypatch):
    monkeypatch.setattr(
        gitlab_client.requests,
        "get",
        lambda url, *, headers, timeout: _Response(
            {
                "id": 52,
                "name": "backend-service",
                "path_with_namespace": "cm/go/backend-service",
                "archived": False,
            }
        ),
    )

    assert gitlab_client.get_accessible_project(52, RUNTIME_CONFIG) == {
        "id": 52,
        "name": "backend-service",
        "path": "cm/go/backend-service",
    }


def test_sync_keeps_only_selected_projects_plus_explicit_work_links(conn, monkeypatch):
    app_settings.set_value(
        conn,
        "integration.gitlab.projects",
        [{"id": 41, "name": "frontend-service", "path": "cm/frontend-service"}],
    )
    manual = conn.execute(
        "INSERT INTO work_items (title,state,position,origin,created_at,updated_at) "
        "VALUES ('Manual MR','next',1000,'manual','now','now')"
    ).lastrowid
    conn.execute(
        "INSERT INTO work_links (work_item_id,source_type,external_id,url,label,created_at) "
        "VALUES (?, 'gitlab_mr', '52!8', '', '', 'now')",
        (manual,),
    )
    automatic = conn.execute(
        "INSERT INTO work_items (title,state,position,origin,created_at,updated_at) "
        "VALUES ('Automatic outside MR','next',2000,'discovery','now','now')"
    ).lastrowid
    conn.execute(
        "INSERT INTO work_links (work_item_id,source_type,external_id,url,label,created_at) "
        "VALUES (?, 'gitlab_mr', '60!9', '', '', 'now')",
        (automatic,),
    )
    conn.commit()
    allowed = gitlab_client._normalize_mr(_mr(project_id=41, iid=17, project="cm/frontend-service"), "author")
    blocked = gitlab_client._normalize_mr(_mr(project_id=60, iid=9, project="other/unrelated"), "author")
    explicit = gitlab_client._normalize_mr(_mr(project_id=52, iid=8, project="other/manual"), "reviewer")
    _patch_successful_required_discovery(monkeypatch, [allowed, blocked])
    monkeypatch.setattr(gitlab_client, "fetch_engaged_mr_ids", lambda *args: set())
    monkeypatch.setattr(
        gitlab_client,
        "_fetch_mr_dict",
        lambda mr_id, role, config=None: dict(explicit) if mr_id == "52!8" else None,
    )
    monkeypatch.setattr(gitlab_client, "_has_my_approval", lambda *args: False)

    assert gitlab_client.sync(conn) == 2
    assert [
        row["mr_id"]
        for row in conn.execute("SELECT mr_id FROM gitlab_mrs_cache ORDER BY mr_id")
    ] == ["41!17", "52!8"]


def test_unselected_auto_proposals_become_inactive_without_deletion(conn):
    allowed = gitlab_client._normalize_mr(
        _mr(project_id=41, iid=17, project="cm/frontend-service"), "reviewer"
    )
    cache_row = {
        **allowed,
        "roles": json.dumps(allowed["roles"]),
        "assignee_usernames": json.dumps(allowed["assignee_usernames"]),
        "reviewer_usernames": json.dumps(allowed["reviewer_usernames"]),
        "labels": json.dumps(allowed["labels"]),
        "stages": "[]",
        "synced_at": "now",
    }
    conn.execute(
        "INSERT INTO gitlab_mrs_cache (mr_id,project,title,state,url,role,roles,author,"
        "author_username,assignee_usernames,reviewer_usernames,source_branch,description,"
        "updated_at,labels,stages,synced_at) VALUES (:mr_id,:project,:title,:state,:url,"
        ":role,:roles,:author,:author_username,:assignee_usernames,:reviewer_usernames,"
        ":source_branch,:description,:updated_at,:labels,:stages,:synced_at)",
        cache_row,
    )
    payloads = [
        {"auto_proposed": True, "related_mr_id": "41!17"},
        {"auto_proposed": True, "related_mr_id": "60!9"},
        {"auto_proposed": False, "related_mr_id": "60!10"},
        {
            "auto_proposed": True,
            "related_mr_id": "41!17",
            "additional_mr_ids": ["60!11"],
        },
    ]
    conn.executemany(
        "INSERT INTO pending_actions (kind,payload_json,status,created_at) "
        "VALUES ('clickup_create_task', ?, 'pending', 'now')",
        [(json.dumps(payload),) for payload in payloads],
    )
    conn.commit()

    assert mr_review_tracker.deactivate_unselected_proposals(conn) == 2
    assert [
        row["status"]
        for row in conn.execute("SELECT status FROM pending_actions ORDER BY id")
    ] == ["pending", "inactive", "pending", "inactive"]
    assert conn.execute("SELECT COUNT(*) FROM pending_actions").fetchone()[0] == 4
    assert mr_review_tracker._already_tracked(conn, "60!9") is False


def test_fetches_exact_self_roles_and_each_tracked_author_across_header_pages(
    conn, monkeypatch
):
    _add_identity(conn, "Morgan", "morgan.dev")
    requests = []

    def get(url, *, headers, params, timeout):
        requests.append((url, headers, dict(params), timeout))
        role_filter = next(
            key
            for key in ("author_username", "assignee_username", "reviewer_username")
            if key in params
        )
        username = params[role_filter]
        page = params["page"]
        if (role_filter, username, page) == ("author_username", "alex", 1):
            return _Response([_mr()], next_page="2")
        if (role_filter, username, page) == ("author_username", "alex", 2):
            return _Response([], next_page="")
        if username == "alex":
            return _Response([_mr()])
        return _Response(
            [_mr(project_id=52, iid=8, project="other/tracked", assignees=(), reviewers=())]
        )

    monkeypatch.setattr(gitlab_client.requests, "get", get)

    rows = gitlab_client.fetch_open_mrs_for_people(conn, RUNTIME_CONFIG)

    assert [row["mr_id"] for row in rows] == ["41!17", "52!8"]
    assert rows[0] == {
        "mr_id": "41!17",
        "project": "group/project",
        "title": "Ship global MR ingestion",
        "state": "opened",
        "url": "https://gitlab.example.com/group/project/-/merge_requests/17",
        "role": "author",
        "roles": ["author", "reviewer", "assignee"],
        "author": "Morgan G",
        "author_username": "morgan.dev",
        "assignee_usernames": ["alex"],
        "reviewer_usernames": ["alex"],
        "source_branch": "feature/global-ingestion_clickup_86abc1234",
        "description": "Keeps the work cache complete.",
        "updated_at": "2026-08-27T10:11:12",
        "labels": ["Ready for QA", "backend"],
    }
    assert [
        (call[2]["author_username"], call[2]["page"])
        for call in requests
        if "author_username" in call[2]
    ] == [("alex", 1), ("alex", 2), ("morgan.dev", 1)]
    for url, headers, params, timeout in requests:
        assert url == "https://gitlab.example.com/api/v4/merge_requests"
        assert headers == {"PRIVATE-TOKEN": "test-token"}
        assert params["scope"] == "all"
        assert params["state"] == "opened"
        assert params["updated_after"] == "2026-06-30T00:00:00"
        assert params["per_page"] == 100
        assert timeout == gitlab_client.TIMEOUT


def test_selected_automatic_work_link_does_not_keep_a_stale_mr_in_cache(
    conn, monkeypatch
):
    """Treating discovery links as explicit would resurrect old open MRs forever."""
    app_settings.set_value(
        conn,
        "integration.gitlab.projects",
        [{"id": 41, "name": "frontend-service", "path": "cm/frontend-service"}],
    )
    automatic = conn.execute(
        "INSERT INTO work_items (title,state,position,origin,created_at,updated_at) "
        "VALUES ('Stale automatic MR','next',1000,'discovery','now','now')"
    ).lastrowid
    manual = conn.execute(
        "INSERT INTO work_items (title,state,position,origin,created_at,updated_at) "
        "VALUES ('Explicit old MR','next',2000,'manual','now','now')"
    ).lastrowid
    conn.executemany(
        "INSERT INTO work_links (work_item_id,source_type,external_id,url,label,created_at) "
        "VALUES (?, 'gitlab_mr', ?, '', '', 'now')",
        [(automatic, "41!9"), (manual, "41!10")],
    )
    conn.commit()
    explicit = gitlab_client._normalize_mr(
        _mr(project_id=41, iid=10, project="cm/frontend-service"), "reviewer"
    )
    _patch_successful_required_discovery(monkeypatch)
    monkeypatch.setattr(gitlab_client, "fetch_engaged_mr_ids", lambda *args: set())
    monkeypatch.setattr(
        gitlab_client,
        "_fetch_mr_dict",
        lambda mr_id, role, config=None: dict(explicit) if mr_id == "41!10" else None,
    )
    monkeypatch.setattr(gitlab_client, "_has_my_approval", lambda *args: False)

    assert gitlab_client.sync(conn) == 1
    assert [
        row["mr_id"]
        for row in conn.execute("SELECT mr_id FROM gitlab_mrs_cache ORDER BY mr_id")
    ] == ["41!10"]


def test_tracked_author_self_reviewer_overlap_stays_reviewer_work(
    conn, monkeypatch
):
    _add_identity(conn, "Morgan", "morgan.dev")
    tracked_review = _mr(project_id=41, iid=17, author="morgan.dev")
    tracked_review["labels"] = []
    self_authored = _mr(project_id=42, iid=18, author="alex")

    def get(url, *, headers, params, timeout):
        query = next(
            (param, params[param])
            for param in (
                "author_username",
                "assignee_username",
                "reviewer_username",
            )
            if param in params
        )
        responses = {
            ("author_username", "alex"): [self_authored],
            ("assignee_username", "alex"): [],
            ("reviewer_username", "alex"): [tracked_review, self_authored],
            ("author_username", "morgan.dev"): [tracked_review],
        }
        return _Response(responses[query])

    monkeypatch.setattr(gitlab_client.requests, "get", get)

    rows = {
        row["mr_id"]: row
        for row in gitlab_client.fetch_open_mrs_for_people(conn, RUNTIME_CONFIG)
    }

    assert rows["41!17"]["roles"] == ["author", "reviewer"]
    assert rows["41!17"]["role"] == "reviewer"
    assert rows["42!18"]["roles"] == ["author", "reviewer"]
    assert rows["42!18"]["role"] == "author"
    review = rows["41!17"]
    conn.execute(
        "INSERT INTO gitlab_mrs_cache"
        " (mr_id,project,title,state,url,role,roles,author,author_username,"
        "  assignee_usernames,reviewer_usernames,source_branch,description,"
        "  updated_at,labels,stages,synced_at)"
        " VALUES (:mr_id,:project,:title,:state,:url,:role,:roles,:author,"
        "  :author_username,:assignee_usernames,:reviewer_usernames,:source_branch,"
        "  :description,:updated_at,:labels,'[]','2026-08-27')",
        {
            **review,
            "roles": json.dumps(review["roles"]),
            "assignee_usernames": json.dumps(review["assignee_usernames"]),
            "reviewer_usernames": json.dumps(review["reviewer_usernames"]),
            "labels": json.dumps(review["labels"]),
        },
    )
    conn.commit()

    assert [row["mr_id"] for row in gitlab_client.stale_review_mrs(conn, 0)] == [
        "41!17"
    ]
    assert gitlab_client._compute_stages(review, False) == ["review_pending"]
    assert mr_review_tracker.propose_review_tasks(conn) == 1


def test_global_fetch_uses_one_immutable_runtime_snapshot_for_all_queries_and_pages(
    conn, monkeypatch
):
    generations = []
    seen = []

    def changing_config():
        generation = len(generations) + 1
        generations.append(generation)
        return MappingProxyType(
            {
                "base_url": f"https://gitlab-{generation}.example.com",
                "username": "alex",
                "token": f"token-{generation}",
            }
        )

    def get(url, *, headers, params, timeout):
        seen.append((url, headers, dict(params)))
        next_page = "2" if params["page"] == 1 else ""
        return _Response([], next_page=next_page)

    monkeypatch.setattr(gitlab_client, "gitlab_config", changing_config)
    monkeypatch.setattr(gitlab_client.requests, "get", get)

    assert gitlab_client.fetch_open_mrs_for_people(conn) == []
    assert generations == [1]
    assert len(seen) == 6
    assert all(call[0].startswith("https://gitlab-1.example.com/") for call in seen)
    assert all(call[1] == {"PRIVATE-TOKEN": "token-1"} for call in seen)


@pytest.mark.parametrize("next_page", ["1", "not-a-page", "1000001"])
def test_global_fetch_rejects_unsafe_pagination_metadata(monkeypatch, next_page):
    monkeypatch.setattr(
        gitlab_client.requests,
        "get",
        lambda *args, **kwargs: _Response([_mr()], next_page=next_page),
    )

    with pytest.raises(ValueError, match="pagination metadata"):
        gitlab_client._fetch_global_open(
            "author_username", "alex", RUNTIME_CONFIG
        )


def test_schema_and_serializer_expose_people_and_role_lists(conn):
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(gitlab_mrs_cache)")
    }
    assert {
        "author_username",
        "assignee_usernames",
        "reviewer_usernames",
        "roles",
    } <= columns
    conn.execute(
        "INSERT INTO gitlab_mrs_cache"
        " (mr_id,project,title,state,url,role,roles,author,author_username,"
        "  assignee_usernames,reviewer_usernames,updated_at,synced_at)"
        " VALUES ('41!17','group/project','MR','opened','https://mr','author',?,"
        "         'Morgan G','morgan.dev',?,?,'2026-08-27','2026-08-27')",
        (
            json.dumps(["author", "reviewer", "assignee"]),
            json.dumps(["alex"]),
            json.dumps(["alex", "sam"]),
        ),
    )
    row = conn.execute(
        "SELECT * FROM gitlab_mrs_cache WHERE mr_id='41!17'"
    ).fetchone()

    serialized = mr_dict(row)

    assert serialized["role"] == "author"
    assert serialized["roles"] == ["author", "reviewer", "assignee"]
    assert serialized["author_username"] == "morgan.dev"
    assert serialized["assignee_usernames"] == ["alex"]
    assert serialized["reviewer_usernames"] == ["alex", "sam"]


def test_serializer_falls_back_to_legacy_role_when_roles_are_not_cached(conn):
    conn.execute(
        "INSERT INTO gitlab_mrs_cache"
        " (mr_id,project,title,state,url,role,author,updated_at,synced_at)"
        " VALUES ('41!18','group/project','Legacy','opened','https://mr/18',"
        "         'reviewer','Morgan G','2026-08-27','2026-08-27')"
    )
    row = conn.execute(
        "SELECT * FROM gitlab_mrs_cache WHERE mr_id='41!18'"
    ).fetchone()

    assert mr_dict(row)["roles"] == ["reviewer"]


def test_ensure_mrs_cached_upserts_a_fully_enriched_normalized_row(
    conn, monkeypatch
):
    conn.execute(
        "INSERT INTO gitlab_mrs_cache"
        " (mr_id,project,title,state,url,role,author,updated_at,synced_at)"
        " VALUES ('41!17','old/project','Old title','opened','https://old',"
        "         'author','Old author','old','old')"
    )
    conn.commit()
    generations = []
    calls = []

    def changing_config():
        generation = len(generations) + 1
        generations.append(generation)
        return MappingProxyType(
            {
                "base_url": f"https://gitlab-{generation}.example.com",
                "username": "alex",
                "token": f"token-{generation}",
            }
        )

    responses = iter(
        [
            _Response({"id": 41}),
            _Response(_mr(project_id=41, iid=17)),
        ]
    )

    def get(url, *, headers, timeout):
        calls.append((url, headers, timeout))
        return next(responses)

    monkeypatch.setattr(gitlab_client, "gitlab_config", changing_config)
    monkeypatch.setattr(gitlab_client.requests, "get", get)

    added = gitlab_client.ensure_mrs_cached(
        conn,
        "See https://gitlab-1.example.com/group/project/-/merge_requests/17",
    )

    assert added == ["41!17"]
    assert generations == [1]
    assert calls == [
        (
            "https://gitlab-1.example.com/api/v4/projects/group%2Fproject",
            {"PRIVATE-TOKEN": "token-1"},
            gitlab_client.TIMEOUT,
        ),
        (
            "https://gitlab-1.example.com/api/v4/projects/41/merge_requests/17",
            {"PRIVATE-TOKEN": "token-1"},
            gitlab_client.TIMEOUT,
        ),
    ]
    serialized = mr_dict(
        conn.execute(
            "SELECT * FROM gitlab_mrs_cache WHERE mr_id='41!17'"
        ).fetchone()
    )
    assert serialized["project"] == "group/project"
    assert serialized["title"] == "Ship global MR ingestion"
    assert serialized["role"] == "reviewer"
    assert serialized["roles"] == ["reviewer"]
    assert serialized["author"] == "Morgan G"
    assert serialized["author_username"] == "morgan.dev"
    assert serialized["assignee_usernames"] == ["alex"]
    assert serialized["reviewer_usernames"] == ["alex"]
    assert serialized["labels"] == ["Ready for QA", "backend"]
    assert serialized["source_branch"] == (
        "feature/global-ingestion_clickup_86abc1234"
    )
    assert serialized["updated_at"] == "2026-08-27T10:11:12"


def test_ensure_mrs_cached_preserves_existing_row_on_normalization_failure(
    conn, monkeypatch
):
    conn.execute(
        "INSERT INTO gitlab_mrs_cache"
        " (mr_id,project,title,state,url,role,author,updated_at,synced_at)"
        " VALUES ('41!17','old/project','Keep me','opened','https://old',"
        "         'reviewer','Old author','old','old')"
    )
    conn.commit()
    responses = iter(
        [
            _Response({"id": 41}),
            _Response({"project_id": 41, "iid": 17, "references": []}),
        ]
    )
    monkeypatch.setattr(gitlab_client, "gitlab_config", lambda: RUNTIME_CONFIG)
    monkeypatch.setattr(
        gitlab_client.requests, "get", lambda *args, **kwargs: next(responses)
    )

    added = gitlab_client.ensure_mrs_cached(
        conn,
        "See https://gitlab.example.com/group/project/-/merge_requests/17",
    )

    assert added == []
    assert tuple(
        conn.execute(
            "SELECT project,title,synced_at FROM gitlab_mrs_cache WHERE mr_id='41!17'"
        ).fetchone()
    ) == ("old/project", "Keep me", "old")


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [("assignees", "alex"), ("reviewers", {"username": "alex"}), ("labels", "qa")],
)
def test_global_fetch_rejects_malformed_people_and_label_lists(
    monkeypatch, field, bad_value
):
    payload = _mr()
    payload[field] = bad_value
    monkeypatch.setattr(
        gitlab_client.requests,
        "get",
        lambda *args, **kwargs: _Response([payload]),
    )

    with pytest.raises(ValueError, match="payload is invalid"):
        gitlab_client._fetch_global_open(
            "author_username", "alex", RUNTIME_CONFIG
        )


def test_failed_global_fetch_preserves_the_entire_existing_cache(conn, monkeypatch):
    conn.executemany(
        "INSERT INTO gitlab_mrs_cache (mr_id,title,state,synced_at) VALUES (?,?,?,?)",
        [
            ("1!2", "Keep me", "opened", "old"),
            ("2!3", "Keep me too", "merged", "old"),
        ],
    )
    conn.commit()
    monkeypatch.setattr(gitlab_client, "gitlab_config", lambda: RUNTIME_CONFIG)
    monkeypatch.setattr(
        gitlab_client,
        "fetch_open_mrs_for_people",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            TimeoutError("private raw failure")
        ),
    )

    with pytest.raises(TimeoutError):
        gitlab_client.sync(conn)

    assert [
        tuple(row)
        for row in conn.execute(
            "SELECT mr_id,title,state,synced_at FROM gitlab_mrs_cache ORDER BY mr_id"
        )
    ] == [
        ("1!2", "Keep me", "opened", "old"),
        ("2!3", "Keep me too", "merged", "old"),
    ]


def test_normalization_failure_preserves_the_entire_existing_cache(conn, monkeypatch):
    conn.execute(
        "INSERT INTO gitlab_mrs_cache (mr_id,title,state,synced_at)"
        " VALUES ('1!2','Keep me','opened','old')"
    )
    conn.commit()
    monkeypatch.setattr(gitlab_client, "gitlab_config", lambda: RUNTIME_CONFIG)
    monkeypatch.setattr(
        gitlab_client.requests,
        "get",
        lambda *args, **kwargs: _Response([{"iid": 2}], next_page=""),
    )

    with pytest.raises(ValueError, match="merge request identity"):
        gitlab_client.sync(conn)

    assert conn.execute(
        "SELECT title FROM gitlab_mrs_cache WHERE mr_id='1!2'"
    ).fetchone()["title"] == "Keep me"


def _patch_successful_required_discovery(monkeypatch, open_rows=()):
    monkeypatch.setattr(gitlab_client, "gitlab_config", lambda: RUNTIME_CONFIG)
    monkeypatch.setattr(
        gitlab_client, "current_username", lambda config=None: "alex"
    )
    monkeypatch.setattr(
        gitlab_client,
        "fetch_open_mrs_for_people",
        lambda conn, config=None, username=None: [dict(row) for row in open_rows],
    )
    monkeypatch.setattr(gitlab_client, "_fetch_role_state", lambda *args, **kwargs: {})
    monkeypatch.setattr(gitlab_client, "_archived_project_ids", lambda *args: set())


def test_engagement_query_failure_preserves_the_entire_existing_cache(
    conn, monkeypatch
):
    conn.execute(
        "INSERT INTO gitlab_mrs_cache (mr_id,title,state,synced_at)"
        " VALUES ('1!2','Keep me','opened','old')"
    )
    conn.commit()
    _patch_successful_required_discovery(monkeypatch)
    monkeypatch.setattr(
        gitlab_client,
        "fetch_engaged_mr_ids",
        lambda *args: (_ for _ in ()).throw(
            gitlab_client.requests.Timeout("private raw failure")
        ),
    )

    with pytest.raises(gitlab_client.requests.Timeout):
        gitlab_client.sync(conn)

    assert conn.execute(
        "SELECT title FROM gitlab_mrs_cache WHERE mr_id='1!2'"
    ).fetchone()["title"] == "Keep me"


def test_tracked_author_self_engagement_uses_engaged_legacy_role(conn, monkeypatch):
    tracked_author = {
        "mr_id": "41!17",
        "project": "group/project",
        "title": "Tracked author's MR",
        "state": "opened",
        "url": "https://gitlab.example.com/group/project/-/merge_requests/17",
        "role": "author",
        "roles": ["author"],
        "author": "Morgan G",
        "author_username": "morgan.dev",
        "assignee_usernames": [],
        "reviewer_usernames": [],
        "source_branch": "feature/tracked",
        "description": "",
        "updated_at": "2026-08-27T10:11:12",
        "labels": [],
    }
    _patch_successful_required_discovery(monkeypatch, [tracked_author])
    monkeypatch.setattr(
        gitlab_client, "fetch_engaged_mr_ids", lambda *args: {"41!17"}
    )
    monkeypatch.setattr(gitlab_client, "_has_my_approval", lambda *args: False)

    assert gitlab_client.sync(conn) == 1

    row = conn.execute(
        "SELECT role,roles FROM gitlab_mrs_cache WHERE mr_id='41!17'"
    ).fetchone()
    assert row["role"] == "engaged"
    assert json.loads(row["roles"]) == ["author", "engaged"]


def test_approval_query_failure_preserves_the_entire_existing_cache(conn, monkeypatch):
    conn.execute(
        "INSERT INTO gitlab_mrs_cache (mr_id,title,state,synced_at)"
        " VALUES ('1!2','Keep me','opened','old')"
    )
    conn.commit()
    reviewer_row = {
        "mr_id": "41!17",
        "project": "group/project",
        "title": "New review",
        "state": "opened",
        "url": "https://gitlab.example.com/group/project/-/merge_requests/17",
        "role": "reviewer",
        "roles": ["reviewer"],
        "author": "Morgan G",
        "author_username": "morgan.dev",
        "assignee_usernames": [],
        "reviewer_usernames": ["alex"],
        "source_branch": "feature/review",
        "description": "",
        "updated_at": "2026-08-27T10:11:12",
        "labels": [],
    }
    _patch_successful_required_discovery(monkeypatch, [reviewer_row])
    monkeypatch.setattr(gitlab_client, "fetch_engaged_mr_ids", lambda *args: set())
    monkeypatch.setattr(
        gitlab_client,
        "_has_my_approval",
        lambda *args: (_ for _ in ()).throw(
            gitlab_client.requests.Timeout("private raw failure")
        ),
    )

    with pytest.raises(gitlab_client.requests.Timeout):
        gitlab_client.sync(conn)

    assert conn.execute(
        "SELECT title FROM gitlab_mrs_cache WHERE mr_id='1!2'"
    ).fetchone()["title"] == "Keep me"


def test_archived_project_query_failure_is_not_treated_as_a_success(monkeypatch):
    monkeypatch.setattr(
        gitlab_client.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            gitlab_client.requests.Timeout("private raw failure")
        ),
    )

    with pytest.raises(gitlab_client.requests.Timeout):
        gitlab_client._archived_project_ids({"41"}, RUNTIME_CONFIG)


def test_atomic_insert_failure_rolls_back_the_entire_cache_replacement(
    conn, monkeypatch
):
    conn.execute(
        "INSERT INTO gitlab_mrs_cache (mr_id,title,state,synced_at)"
        " VALUES ('1!2','Keep me','opened','old')"
    )
    conn.execute(
        "CREATE TEMP TRIGGER reject_new_gitlab_cache BEFORE INSERT ON gitlab_mrs_cache"
        " WHEN NEW.mr_id = '41!17' BEGIN SELECT RAISE(ABORT, 'reject'); END"
    )
    conn.commit()
    author_row = {
        "mr_id": "41!17",
        "project": "group/project",
        "title": "New author work",
        "state": "opened",
        "url": "https://gitlab.example.com/group/project/-/merge_requests/17",
        "role": "author",
        "roles": ["author"],
        "author": "Alex",
        "author_username": "alex",
        "assignee_usernames": [],
        "reviewer_usernames": [],
        "source_branch": "feature/work",
        "description": "",
        "updated_at": "2026-08-27T10:11:12",
        "labels": [],
    }
    _patch_successful_required_discovery(monkeypatch, [author_row])
    monkeypatch.setattr(gitlab_client, "fetch_engaged_mr_ids", lambda *args: set())

    with pytest.raises(Exception, match="reject"):
        gitlab_client.sync(conn)

    assert [
        tuple(row)
        for row in conn.execute(
            "SELECT mr_id,title,synced_at FROM gitlab_mrs_cache ORDER BY mr_id"
        )
    ] == [("1!2", "Keep me", "old")]
