"""GitLab REST connector — read-only in v1. Caches open MRs where the user
is author or reviewer, plus recently-merged ones (so completion sync can
detect MR-merge → close-task)."""
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from types import MappingProxyType

import requests

from ..config import settings
from ..models import now_iso
from . import app_settings, external_errors, secret_store

# Parallel-worker cap for the per-MR approval-comment scan during sync.
# Matches the ClickUp referenced-task fetch cap — GitLab and ClickUp both
# take the traffic without complaint at 10 concurrent requests.
REVIEW_SCAN_WORKERS = 10

log = logging.getLogger("watson.gitlab")

TIMEOUT = 20


def _get(url, **kwargs):
    response = requests.get(url, **kwargs)
    authorization = kwargs.get('headers', {}).get('Authorization', '')
    if authorization.startswith('Bearer ') and response.status_code == 401:
        from . import gitlab_oauth
        gitlab_oauth.mark_unauthorized(authorization.removeprefix('Bearer '))
    return response

# Branches encode the related ClickUp task id after a `_clickup` tag, e.g.
# `feature/foo_clickup_86d3by9x6` or `bugfix/_clickup-86abc/xyz`. We accept an
# optional separator (`_`, `-`, `/`) and at least a few alphanumerics — the
# captured id is later validated against the managed_tasks table, so false
# positives don't write anything.
CLICKUP_BRANCH_RE = re.compile(r"_clickup[_\-/]?([A-Za-z0-9]{4,})", re.I)

# ClickUp task URLs come in several shapes:
#   https://app.clickup.com/t/86d3m9u6j
#   https://app.clickup.com/t/606026/86d3m9u6j           ← workspace THEN task
#   https://app.clickup.com/9012345/t/86abc1234           ← workspace BEFORE /t/
# Real task ids are 8-10 alphanumeric (start with letters); workspace ids are
# purely numeric. The regex consumes an optional numeric workspace segment on
# either side of /t/ before capturing the real id.
CLICKUP_URL_RE = re.compile(
    r"https?://[^/\s]*clickup\.com/(?:\d+/)?t/(?:\d+/)?([A-Za-z0-9]{4,})", re.I
)


def clickup_id_from_branch(branch):
    if not branch:
        return None
    m = CLICKUP_BRANCH_RE.search(branch)
    return m.group(1) if m else None


def clickup_id_from_description(text):
    """Fallback signal when the branch name doesn't carry the `_clickup<id>`
    tag (typos, forgotten convention). If the MR description links to a ClickUp
    task, use that. Returns the first id found or None."""
    if not text:
        return None
    m = CLICKUP_URL_RE.search(text)
    return m.group(1) if m else None


def clickup_id_from_mr(row_or_dict):
    """Combined helper: try branch first (highest precision), fall back to
    description. Accepts either a sqlite3.Row or a dict."""
    def _get(key):
        try:
            return row_or_dict[key]
        except (KeyError, IndexError):
            return None
    return (clickup_id_from_branch(_get("source_branch"))
            or clickup_id_from_description(_get("description")))


# matches GitLab MR URLs like ".../projects/.../merge_requests/6315" or the
# typical "https://<host>/group/project/-/merge_requests/6315" form.
_MR_URL_RE = re.compile(
    r"https?://[^/\s]+/([^\s]+?)/-/merge_requests/(\d+)", re.I
)


def fetch_mr_by_path(path_with_namespace: str, iid, config=None):
    """Fetch a single MR + its project id by namespaced path + iid (read-only).
    Returns a cache-row dict, or None on any failure."""
    config = config or gitlab_config()
    if not configured(config):
        return None
    try:
        from urllib.parse import quote
        encoded = quote(path_with_namespace, safe="")
        proj = _get(_api(f"/projects/{encoded}", config), headers=_headers(config),
                            timeout=TIMEOUT)
        proj.raise_for_status()
        pid = proj.json()["id"]
        r = _get(_api(f"/projects/{pid}/merge_requests/{iid}", config),
                         headers=_headers(config), timeout=TIMEOUT)
        r.raise_for_status()
        mr = r.json()
        row = _normalize_mr(mr, "reviewer")
        if row["mr_id"] != f"{pid}!{iid}":
            raise ValueError("GitLab merge request identity is invalid")
        if row["project"] == str(pid):
            row["project"] = path_with_namespace
    except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
        external_errors.log_failure(
            log, f"ad-hoc MR fetch {path_with_namespace}!{iid}", "gitlab", exc
        )
        return None
    return row


def ensure_mrs_cached(conn, text: str) -> list:
    """Find any MR URLs in `text` that aren't in the cache yet, fetch them, and
    insert them. Returns the list of newly-cached mr_ids — best-effort, swallows
    individual failures so a classification never crashes on a flaky URL."""
    found = _MR_URL_RE.findall(text or "")
    if not found:
        return []
    config = gitlab_config()
    if not configured(config):
        return []
    added = []
    for path, iid in found:
        # the URL might already be in the cache under its `<project_id>!<iid>` form,
        # but we don't know the project_id without a lookup — so first scan by URL.
        if conn.execute(
            "SELECT 1 FROM gitlab_mrs_cache WHERE url LIKE ?",
            (f"%/{path}/-/merge_requests/{iid}",),
        ).fetchone():
            continue
        row = fetch_mr_by_path(path, iid, config)
        if not row:
            continue
        cache_row = {
            **row,
            "roles": json.dumps(row.get("roles") or [row["role"]]),
            "assignee_usernames": json.dumps(row.get("assignee_usernames") or []),
            "reviewer_usernames": json.dumps(row.get("reviewer_usernames") or []),
            "labels": json.dumps(row.get("labels") or []),
            "stages": json.dumps([]),
            "synced_at": now_iso(),
        }
        conn.execute(
            "INSERT OR REPLACE INTO gitlab_mrs_cache (mr_id, project, title, state,"
            " url, role, roles, author, author_username, assignee_usernames,"
            " reviewer_usernames, source_branch, description, updated_at, labels,"
            " stages, synced_at)"
            " VALUES (:mr_id, :project, :title, :state, :url, :role, :roles, :author,"
            " :author_username, :assignee_usernames, :reviewer_usernames,"
            " :source_branch, :description, :updated_at, :labels, :stages, :synced_at)",
            cache_row,
        )
        added.append(row["mr_id"])
    if added:
        conn.commit()
        log.info("ensure_mrs_cached: added %s", added)
    return added


def gitlab_config() -> dict:
    """Resolve the current GitLab settings without trusting stale boot state."""
    if app_settings.integration_disabled("gitlab"):
        return MappingProxyType({"base_url": "", "username": "", "token": ""})
    try:
        base_url = app_settings.effective_nonsecret(
            "integration.gitlab.base_url", settings.gitlab_base_url
        )
        username = app_settings.effective_nonsecret(
            "integration.gitlab.username", settings.gitlab_username
        )
        token = secret_store.effective_secret("gitlab.token", settings.gitlab_token)
        from . import gitlab_oauth
        oauth_token = gitlab_oauth.access_token(base_url)
        if oauth_token is not None:
            token = oauth_token
    except Exception:
        return MappingProxyType({"base_url": "", "username": "", "token": ""})
    return MappingProxyType({
        "base_url": str(base_url or ""),
        "username": str(username or ""),
        "token": token,
        **({"oauth": True} if oauth_token is not None else {}),
    })


def configured(config=None) -> bool:
    # username is optional — derived from the token when not set
    config = config or gitlab_config()
    return bool(config["base_url"] and config["token"])


def _require_config(config=None):
    config = config or gitlab_config()
    if not configured(config):
        raise RuntimeError(
            "GitLab is not configured (GITLAB_BASE_URL / GITLAB_TOKEN)"
        )
    return config


def _api(path: str, config=None) -> str:
    # tolerate base urls given with or without a trailing /api/v4
    config = _require_config(config)
    base = config["base_url"].rstrip("/")
    if base.endswith("/api/v4"):
        base = base[: -len("/api/v4")]
    return base + "/api/v4" + path


def _headers(config=None) -> dict:
    config = _require_config(config)
    if config.get('oauth'):
        return {"Authorization": "Bearer " + config["token"]}
    return {"PRIVATE-TOKEN": config["token"]}


def current_username(config=None) -> str:
    """Resolve whose MRs to fetch: the configured name, else the token's owner."""
    config = _require_config(config)
    username = config["username"].strip()
    if username:
        return username
    resp = _get(
        _api("/user", config), headers=_headers(config), timeout=TIMEOUT
    )
    resp.raise_for_status()
    return resp.json()["username"]


def current_user(config=None) -> dict:
    config = _require_config(config)
    resp = _get(
        _api("/user", config), headers=_headers(config), timeout=TIMEOUT
    )
    resp.raise_for_status()
    return resp.json()


def _normalize_project(project) -> dict:
    if not isinstance(project, dict):
        raise ValueError("GitLab project payload is invalid")
    project_id = project.get("id")
    name = str(project.get("name") or "").strip()
    path = str(project.get("path_with_namespace") or "").strip()
    if not isinstance(project_id, int) or project_id <= 0 or not name or not path:
        raise ValueError("GitLab project payload is invalid")
    if project.get("archived", False):
        raise ValueError("GitLab project is archived")
    return {"id": project_id, "name": name, "path": path}


def get_accessible_project(project_id: int, config=None) -> dict:
    """Resolve one accessible, non-archived project by stable GitLab id."""
    config = _require_config(config)
    response = _get(
        _api(f"/projects/{project_id}", config),
        headers=_headers(config),
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    project = _normalize_project(response.json())
    if project["id"] != project_id:
        raise ValueError("GitLab project identity is invalid")
    return project


def list_accessible_projects(config=None, *, search="") -> list[dict]:
    """Search non-archived projects visible to the configured token."""
    config = _require_config(config)
    projects = []
    page = 1
    while True:
        response = _get(
            _api("/projects", config),
            headers=_headers(config),
            params={
                "membership": True,
                "simple": True,
                "archived": False,
                "order_by": "path",
                "sort": "asc",
                "per_page": 100,
                "page": page,
                **({"search": search.strip()} if search.strip() else {}),
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        batch = response.json()
        if not isinstance(batch, list):
            raise ValueError("GitLab project payload is invalid")
        for project in batch:
            if project.get("archived", False):
                continue
            projects.append(_normalize_project(project))
        next_page = _next_global_page(response.headers, page)
        if next_page is None:
            return projects
        page = next_page


def token_scopes(config=None) -> list:
    """Scopes on the configured token (read_api, api, …); [] if unavailable."""
    try:
        config = _require_config(config)
        resp = _get(
            _api("/personal_access_tokens/self", config),
            headers=_headers(config),
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("scopes", [])
    except (RuntimeError, requests.RequestException):
        return []


def drop_self_as_reviewer(mr_id: str, my_id: int, config=None) -> bool:
    """Remove the current user from an MR's reviewers. Returns True if a change
    was made, False if the user wasn't a reviewer. Raises on API/permission error."""
    config = _require_config(config)
    project_id, iid = mr_id.split("!")
    r = _get(
        _api(f"/projects/{project_id}/merge_requests/{iid}", config),
        headers=_headers(config), timeout=TIMEOUT
    )
    r.raise_for_status()
    current_ids = [u["id"] for u in (r.json().get("reviewers") or [])]
    if my_id not in current_ids:
        return False
    remaining = [i for i in current_ids if i != my_id]
    resp = requests.put(
        _api(f"/projects/{project_id}/merge_requests/{iid}", config),
        headers=_headers(config),
        # empty list doesn't reliably clear across GitLab versions; [0] does
        json={"reviewer_ids": remaining or [0]},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return True


def authored_open_mrs(conn) -> list:
    """Cached open MRs the user authored."""
    return conn.execute(
        "SELECT * FROM gitlab_mrs_cache WHERE role = 'author' AND state = 'opened'"
        " ORDER BY updated_at"
    ).fetchall()


def close_mr(mr_id: str, config=None) -> str:
    """Close an MR (reversible — can be reopened). Returns the new state."""
    config = _require_config(config)
    project_id, iid = mr_id.split("!")
    resp = requests.put(
        _api(f"/projects/{project_id}/merge_requests/{iid}", config),
        headers=_headers(config),
        json={"state_event": "close"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("state", "")


def stale_review_mrs(conn, days: int) -> list:
    """Cached MRs where the user is reviewer and there's been no activity in >days."""
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT * FROM gitlab_mrs_cache WHERE role IN ('reviewer','engaged') AND state = 'opened'"
        " AND updated_at <= ? ORDER BY updated_at",
        (cutoff,),
    ).fetchall()
    return rows


def _archived_project_ids(project_ids: set, config=None) -> set:
    """Of the given project ids, which are archived (read-only, never actionable)."""
    archived = set()
    config = _require_config(config)
    for pid in project_ids:
        r = _get(
            _api(f"/projects/{pid}", config),
            headers=_headers(config),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        project = r.json()
        if not isinstance(project, dict):
            raise ValueError("GitLab project payload is invalid")
        if project.get("archived"):
            archived.add(str(pid))
    return archived


MERGED_LOOKBACK_DAYS = 30  # how far back we keep merged MRs for completion sync
# The local Watson instance intentionally ignores older open work. GitLab can
# leave reviewer assignments open for years, which otherwise floods Team with
# stale cards that are no longer actionable.
OPEN_MR_UPDATED_AFTER = "2026-06-30T00:00:00"
MAX_GLOBAL_MR_PAGES = 1000
_ROLE_PRECEDENCE = ("author", "reviewer", "assignee", "engaged")
_SELF_ROLE_PRECEDENCE = ("reviewer", "assignee", "author", "engaged")
_GLOBAL_FILTER_ROLES = {
    "author_username": "author",
    "reviewer_username": "reviewer",
    "assignee_username": "assignee",
}


def tracked_gitlab_usernames(conn) -> list[str]:
    """Explicit GitLab identities belonging to people the user tracks."""
    return [
        row["external_id"].strip()
        for row in conn.execute(
            "SELECT DISTINCT pi.external_id"
            " FROM person_identities pi"
            " JOIN people p ON p.id = pi.person_id"
            " WHERE p.is_tracked = 1 AND pi.source = 'gitlab'"
            "   AND TRIM(pi.external_id) <> ''"
            " ORDER BY pi.external_id COLLATE NOCASE"
        )
    ]


def _usernames(people) -> list[str]:
    if people is None:
        return []
    if not isinstance(people, list) or not all(
        isinstance(person, dict) for person in people
    ):
        raise ValueError("GitLab merge request payload is invalid")
    usernames = {
        str(person.get("username") or "").strip()
        for person in people
    }
    usernames.discard("")
    return sorted(usernames, key=str.casefold)


def _string_list(values) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or not all(
        isinstance(value, str) for value in values
    ):
        raise ValueError("GitLab merge request payload is invalid")
    return values


def _project_path(mr: dict, project_id: str) -> str:
    references = mr.get("references")
    if references is None:
        references = {}
    if not isinstance(references, dict):
        raise ValueError("GitLab merge request payload is invalid")
    full_ref = str(references.get("full") or "")
    if "!" in full_ref:
        path = full_ref.rsplit("!", 1)[0].strip()
        if path:
            return path
    url = str(mr.get("web_url") or "")
    if "/-/merge_requests/" in url:
        path = url.split("//", 1)[-1].split("/", 1)[-1]
        path = path.split("/-/merge_requests/", 1)[0].strip()
        if path:
            return path
    return project_id


def _normalize_mr(mr: dict, role: str, default_state="opened") -> dict:
    """Shape one global GitLab response into the stable cache contract."""
    if not isinstance(mr, dict):
        raise ValueError("GitLab merge request payload is invalid")
    project_id = str(mr.get("project_id") or "").strip()
    iid = str(mr.get("iid") or "").strip()
    if not project_id or not iid or "!" in project_id or "!" in iid:
        raise ValueError("GitLab merge request identity is invalid")
    if role not in _ROLE_PRECEDENCE:
        raise ValueError("GitLab merge request role is invalid")
    author = mr.get("author") or {}
    if not isinstance(author, dict):
        raise ValueError("GitLab merge request author is invalid")
    author_username = str(author.get("username") or "").strip()
    return {
        "mr_id": f"{project_id}!{iid}",
        "project": _project_path(mr, project_id),
        "title": str(mr.get("title") or ""),
        "state": str(mr.get("state") or default_state),
        "url": str(mr.get("web_url") or ""),
        "role": role,
        "roles": [role],
        "author": str(author.get("name") or author_username),
        "author_username": author_username,
        "assignee_usernames": _usernames(mr.get("assignees")),
        "reviewer_usernames": _usernames(mr.get("reviewers")),
        "source_branch": str(mr.get("source_branch") or ""),
        "description": str(mr.get("description") or ""),
        "updated_at": str(mr.get("updated_at") or "")[:19],
        "labels": _string_list(mr.get("labels")),
    }


def _merge_mr(existing: dict | None, incoming: dict) -> dict:
    if existing is None:
        merged = dict(incoming)
        roles = set(incoming.get("roles") or [incoming["role"]])
        self_roles = set(incoming.get("_self_roles") or [])
    else:
        merged = existing
        roles = set(existing.get("roles") or [existing["role"]])
        roles.update(incoming.get("roles") or [incoming["role"]])
        self_roles = set(existing.get("_self_roles") or [])
        self_roles.update(incoming.get("_self_roles") or [])
    merged["roles"] = [role for role in _ROLE_PRECEDENCE if role in roles]
    merged["_self_roles"] = [
        role for role in _SELF_ROLE_PRECEDENCE if role in self_roles
    ]
    # An MR authored by the current user remains own work even if GitLab also
    # returns it from reviewer/assignee filters. Otherwise self-query
    # provenance wins over a tracked person's author query so legacy review
    # consumers continue to see assigned reviews.
    if "author" in self_roles:
        merged["role"] = "author"
    elif merged["_self_roles"]:
        merged["role"] = merged["_self_roles"][0]
    else:
        merged["role"] = merged["roles"][0]
    return merged


def _next_global_page(headers, current_page: int) -> int | None:
    raw = str((headers or {}).get("X-Next-Page") or "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise ValueError("GitLab pagination metadata is invalid")
    next_page = int(raw)
    if (
        next_page <= current_page
        or next_page > MAX_GLOBAL_MR_PAGES
    ):
        raise ValueError("GitLab pagination metadata is invalid")
    return next_page


def _fetch_global_open(
    param: str,
    username: str,
    config=None,
    *,
    state="opened",
    extra_params=None,
) -> list[dict]:
    """Fetch one filtered global MR query, following bounded header pages."""
    config = _require_config(config)
    role = _GLOBAL_FILTER_ROLES.get(param)
    if not role:
        raise ValueError("GitLab merge request filter is invalid")
    page = 1
    rows = []
    base = {
        "scope": "all",
        "state": state,
        "per_page": 100,
        param: username,
        **(extra_params or {}),
    }
    while True:
        response = _get(
            _api("/merge_requests", config),
            headers=_headers(config),
            params={**base, "page": page},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        batch = response.json()
        if not isinstance(batch, list):
            raise ValueError("GitLab merge request payload is invalid")
        rows.extend(_normalize_mr(mr, role, state) for mr in batch)
        next_page = _next_global_page(response.headers, page)
        if next_page is None:
            return rows
        page = next_page


def fetch_open_mrs_for_people(conn, config=None, username=None) -> list[dict]:
    """Collect self work plus authored work for each tracked GitLab identity."""
    config = _require_config(config)
    username = (username or current_username(config)).strip()
    queries = [
        ("author_username", username),
        ("assignee_username", username),
        ("reviewer_username", username),
    ]
    seen_usernames = {username.casefold()}
    for tracked in tracked_gitlab_usernames(conn):
        if tracked.casefold() in seen_usernames:
            continue
        seen_usernames.add(tracked.casefold())
        queries.append(("author_username", tracked))

    merged = {}
    for param, query_username in queries:
        for row in _fetch_global_open(
            param,
            query_username,
            config,
            extra_params={"updated_after": OPEN_MR_UPDATED_AFTER},
        ):
            candidate = dict(row)
            if query_username.casefold() == username.casefold():
                candidate["_self_roles"] = [candidate["role"]]
            merged[row["mr_id"]] = _merge_mr(
                merged.get(row["mr_id"]), candidate
            )
    rows = list(merged.values())
    for row in rows:
        row.pop("_self_roles", None)
    return rows


def _fetch_mr_dict(mr_id: str, role: str, config=None) -> dict | None:
    """Fetch a single MR by `<project_id>!<iid>` and shape it as a cache row.
    Returns None on any API failure — caller decides whether to log."""
    config = _require_config(config)
    try:
        pid, iid = mr_id.split("!", 1)
        r = _get(_api(f"/projects/{pid}/merge_requests/{iid}", config),
                         headers=_headers(config), timeout=TIMEOUT)
        r.raise_for_status()
        mr = r.json()
    except (requests.RequestException, ValueError):
        return None
    normalized = _normalize_mr(mr, role)
    if normalized["mr_id"] != mr_id:
        raise ValueError("GitLab merge request identity is invalid")
    return normalized


def fetch_engaged_mr_ids(after_iso: str, config=None) -> set:
    """MR ids the user has touched (commented on, approved/unapproved, accepted,
    closed/reopened) since `after_iso`. Returns `{"<project_id>!<iid>", ...}`.
    Survives MR reassignment because GitLab's event log is immutable."""
    config = _require_config(config)
    user_id = current_user(config).get("id")
    if not user_id:
        return set()
    ids: set = set()
    page = 1
    base = {"after": after_iso, "per_page": 100}
    while True:
        resp = _get(
            _api(f"/users/{user_id}/events", config), headers=_headers(config),
            params={**base, "page": page}, timeout=TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json()
        for ev in batch:
            project_id = ev.get("project_id")
            if not project_id:
                continue
            target_type = ev.get("target_type")
            action = (ev.get("action_name") or "").lower()
            if target_type == "Note":
                note = ev.get("note") or {}
                if note.get("noteable_type") == "MergeRequest" and note.get("noteable_iid"):
                    ids.add(f"{project_id}!{note['noteable_iid']}")
            elif target_type == "MergeRequest":
                # approved / unapproved / accepted / closed / reopened on MRs
                if action in {"approved", "unapproved", "accepted", "closed", "reopened"} and ev.get("target_iid"):
                    ids.add(f"{project_id}!{ev['target_iid']}")
        if len(batch) < 100:
            break
        page += 1
    return ids


def _fetch_role_state(
    role, param, username, state, extra_params=None, config=None
) -> dict:
    """Paginate /merge_requests for one (role × state) combination."""
    out = {}
    for row in _fetch_global_open(
        param,
        username,
        config,
        state=state,
        extra_params=extra_params,
    ):
        if row["role"] != role:
            raise ValueError("GitLab merge request role is invalid")
        row["_self_roles"] = [role]
        out[row["mr_id"]] = _merge_mr(out.get(row["mr_id"]), row)
    return out


def fetch_mrs(config=None) -> list:
    config = _require_config(config)
    username = current_username(config)
    mrs = {}
    # 1) the active queue (open MRs the user owns or is reviewing)
    for role, param in (("author", "author_username"), ("reviewer", "reviewer_username")):
        for mr_id, row in _fetch_role_state(
            role, param, username, "opened", config=config
        ).items():
            mrs[mr_id] = _merge_mr(mrs.get(mr_id), row)
    # 2) recently merged MRs — needed to detect MR-merge → close-task without
    #    unbounded growth. Bounded to MERGED_LOOKBACK_DAYS so any backlog stays small.
    updated_after = (datetime.now() - timedelta(days=MERGED_LOOKBACK_DAYS)).isoformat(timespec="seconds")
    for role, param in (("author", "author_username"), ("reviewer", "reviewer_username")):
        # author wins if an MR appears in both queries
        for mr_id, mr in _fetch_role_state(role, param, username, "merged",
                                           {"updated_after": updated_after},
                                           config).items():
            mrs[mr_id] = _merge_mr(mrs.get(mr_id), mr)

    # drop MRs in archived (read-only) projects — they're never actionable
    archived = _archived_project_ids(
        {mid.split("!")[0] for mid in mrs}, config
    )
    rows = [m for mid, m in mrs.items() if mid.split("!")[0] not in archived]
    for row in rows:
        row.pop("_self_roles", None)
    return rows


# Approval language on informal comments (the user's team doesn't always
# click the formal GitLab Approve button; a "looks good" comment is the
# actual sign-off). Kept intentionally conservative — a bare emoji doesn't
# count, but explicit phrases do. Match case-insensitive substring.
_APPROVAL_PATTERNS = (
    "looks good",
    "look good",
    "changes look good",
    "lgtm",
    "ship it",
    "approved",
    "no more comments",
)


def _has_my_approval(mr_id: str, my_username: str, config=None) -> bool:
    """Scan the MR's recent notes for a comment from the current user with
    approval language. Called during sync for opened reviewer/engaged MRs
    so the Today badge can distinguish "reviewed by me" from "review pending".

    Cost: one API call per MR. Called in parallel by sync() via a thread
    pool, so the wall-clock hit is bounded.
    """
    if not my_username:
        return False
    config = _require_config(config)
    pid, iid = mr_id.split("!", 1)
    r = _get(
        _api(f"/projects/{pid}/merge_requests/{iid}/notes", config),
        headers=_headers(config),
        params={"sort": "desc", "per_page": 30, "order_by": "created_at"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    notes = r.json() or []
    if not isinstance(notes, list):
        raise ValueError("GitLab merge request notes payload is invalid")
    for n in notes:
        author = n.get("author") or {}
        if author.get("username") != my_username:
            continue
        if n.get("system"):  # system notes ("marked as draft", "approved") skip — we handle approvals separately
            continue
        body = (n.get("body") or "").lower()
        if any(pat in body for pat in _APPROVAL_PATTERNS):
            return True
    return False


def _compute_stages(row: dict, reviewed_by_me: bool) -> list:
    """Lifecycle tags an MR carries at sync time. Any subset of
      {merged, qa, reviewed_by_me, review_pending, closed}
    can appear together — e.g. ['reviewed_by_me', 'qa'] means "I signed
    off, and the MR then moved to QA hand-off". Terminal states (merged /
    closed) stand alone. Author's own MRs get an empty list — they're not
    in the reviewer's queue.

    Returned as a list ordered left→right roughly in workflow order:
    reviewed → qa → merged. The frontend renders badges in this order so
    the reader sees progression at a glance.
    """
    if row.get("state") == "merged":
        return ["merged"]
    if row.get("state") == "closed":
        return ["closed"]
    if row.get("role") == "author":
        return []
    tags = []
    if reviewed_by_me:
        tags.append("reviewed_by_me")
    qa_needle = (settings.gitlab_qa_label_substring or "").strip().lower()
    if qa_needle:
        labels_lower = [str(l).lower() for l in (row.get("labels") or [])]
        if any(qa_needle in l for l in labels_lower):
            tags.append("qa")
    if not tags:
        tags.append("review_pending")
    return tags


def _active_discovered_mr_ids(conn, selected_ids: set[str] | None = None) -> set[str]:
    """Keep lifecycle state for active, in-scope discovered work complete.

    Closed MRs disappear from open-MR discovery. Their absence must not leave
    linked cards permanently unknown, nor resurrect unrelated historical work.
    """
    from . import work_items

    referenced = set()
    for item in conn.execute(
        "SELECT wi.*, p.is_self, p.is_tracked FROM work_items wi "
        "LEFT JOIN people p ON p.id=wi.owner_person_id "
        "WHERE wi.origin='discovery' AND wi.state != 'done' "
        "AND wi.completed_at IS NULL"
    ).fetchall():
        if not (item['is_self'] or item['is_tracked'] or work_items._discovery_is_team_relevant(conn, item)):
            continue
        referenced.update(
            row['external_id'] for row in conn.execute(
                "SELECT external_id FROM work_links WHERE work_item_id=? "
                "AND source_type='gitlab_mr' AND TRIM(external_id) <> ''",
                (item['id'],),
            ) if _project_is_selected(row['external_id'], selected_ids)
        )
    return referenced


def _referenced_mr_ids(conn, selected_ids: set[str] | None = None) -> set[str]:
    referenced = set()
    for row in conn.execute(
        "SELECT related_mr_id, additional_mr_ids FROM managed_tasks"
    ):
        if row["related_mr_id"] and _project_is_selected(
            row["related_mr_id"], selected_ids
        ):
            referenced.add(row["related_mr_id"])
        try:
            additional = json.loads(row["additional_mr_ids"] or "[]")
        except (json.JSONDecodeError, TypeError):
            additional = []
        if isinstance(additional, list):
            referenced.update(
                mr_id for mr_id in additional
                if isinstance(mr_id, str)
                and mr_id
                and _project_is_selected(mr_id, selected_ids)
            )
    referenced.update(
        row["external_id"]
        for row in conn.execute(
            "SELECT DISTINCT wl.external_id, wi.origin FROM work_links wl "
            "JOIN work_items wi ON wi.id=wl.work_item_id "
            "WHERE wl.source_type='gitlab_mr' AND TRIM(wl.external_id) <> '' "
            "AND wi.origin='manual'"
        )
        if row["origin"] == "manual"
    )
    return referenced


def selected_project_ids(conn) -> set[str] | None:
    """Return the configured automatic-discovery allowlist.

    ``None`` preserves legacy installations that have not opened the new
    repository setting yet. New GitLab connections persist an explicit empty
    list, which means no automatic discovery until repositories are selected.
    """
    projects = app_settings.get(conn, "integration.gitlab.projects")
    if projects is None:
        return None
    if not isinstance(projects, list):
        return set()
    return {
        str(project["id"])
        for project in projects
        if isinstance(project, dict)
        and isinstance(project.get("id"), int)
        and project["id"] > 0
    }


def _project_is_selected(mr_id: str, selected_ids: set[str] | None) -> bool:
    return selected_ids is None or mr_id.split("!", 1)[0] in selected_ids


def _self_roles_for_row(row: dict, username: str) -> list[str]:
    username_key = (username or "").casefold()
    if not username_key:
        return []
    roles = set()
    if str(row.get("author_username") or "").casefold() == username_key:
        roles.add("author")
    if any(
        str(value).casefold() == username_key
        for value in (row.get("reviewer_usernames") or [])
    ):
        roles.add("reviewer")
    if any(
        str(value).casefold() == username_key
        for value in (row.get("assignee_usernames") or [])
    ):
        roles.add("assignee")
    # The filtered query itself is authoritative even on GitLab versions that
    # omit reviewer/assignee arrays from the global response.
    if row.get("role") in ("reviewer", "assignee"):
        roles.add(row["role"])
    return [role for role in _SELF_ROLE_PRECEDENCE if role in roles]


def sync(conn) -> int:
    config = _require_config()
    username = current_username(config).strip()
    selected_ids = selected_project_ids(conn)
    merged_by_id = {}
    for row in fetch_open_mrs_for_people(conn, config, username):
        if not _project_is_selected(row["mr_id"], selected_ids):
            continue
        candidate = dict(row)
        candidate["_self_roles"] = _self_roles_for_row(candidate, username)
        merged_by_id[row["mr_id"]] = _merge_mr(
            merged_by_id.get(row["mr_id"]), candidate
        )

    updated_after = (
        datetime.now() - timedelta(days=MERGED_LOOKBACK_DAYS)
    ).isoformat(timespec="seconds")
    for role, param in (
        ("author", "author_username"),
        ("reviewer", "reviewer_username"),
    ):
        for mr_id, row in _fetch_role_state(
            role,
            param,
            username,
            "merged",
            {"updated_after": updated_after},
            config,
        ).items():
            if not _project_is_selected(mr_id, selected_ids):
                continue
            merged_by_id[mr_id] = _merge_mr(merged_by_id.get(mr_id), row)

    discovered_references = _active_discovered_mr_ids(conn, selected_ids)
    archived = _archived_project_ids(
        {mr_id.split("!", 1)[0] for mr_id in set(merged_by_id) | discovered_references}, config
    )
    discovered_references = {
        mr_id for mr_id in discovered_references
        if mr_id.split("!", 1)[0] not in archived
    }
    for mr_id in list(merged_by_id):
        if mr_id.split("!", 1)[0] in archived:
            del merged_by_id[mr_id]

    synced_at = now_iso()

    # MRs referenced by Watson's managed tasks must survive the sync even if
    # the user isn't formally author/reviewer (e.g. they're informally reviewing).
    # Re-fetch each so we get fresh state (state, title, etc.).
    referenced = _referenced_mr_ids(conn, selected_ids)
    fetched_ids = set(merged_by_id)
    referenced_added = 0
    for mr_id in sorted((referenced | discovered_references) - fetched_ids):
        row = _fetch_mr_dict(mr_id, "reviewer" if mr_id in referenced else "author", config)
        if not row:
            raise RuntimeError("GitLab linked merge request refresh failed")
        # Tracking a teammate's card is not evidence that the local user is
        # its reviewer. Preserve the legacy explicit-reference behavior only.
        row["_self_roles"] = ["reviewer"] if mr_id in referenced else _self_roles_for_row(row, username)
        merged_by_id[mr_id] = _merge_mr(merged_by_id.get(mr_id), row)
        fetched_ids.add(mr_id)
        referenced_added += 1

    # MRs the user has engaged with (comments, approvals) but isn't formally
    # assigned to — survives reassignment because GitLab's event log is immutable.
    engaged_added = 0
    after = (
        datetime.now() - timedelta(days=settings.engagement_lookback_days)
    ).date().isoformat()
    engaged_ids = fetch_engaged_mr_ids(after, config)
    for mr_id in engaged_ids & fetched_ids:
        merged_by_id[mr_id] = _merge_mr(
            merged_by_id[mr_id],
            {
                "role": "engaged",
                "roles": ["engaged"],
                "_self_roles": ["engaged"],
            },
        )
    new_engaged_ids = {
        mr_id for mr_id in engaged_ids - fetched_ids
        if _project_is_selected(mr_id, selected_ids)
    }
    engaged_archived = _archived_project_ids(
        {mid.split("!")[0] for mid in new_engaged_ids}, config
    )
    for mr_id in new_engaged_ids:
        if mr_id.split("!")[0] in engaged_archived:
            continue
        row = _fetch_mr_dict(mr_id, "engaged", config)
        if not row:
            raise RuntimeError("GitLab engaged merge request refresh failed")
        row["_self_roles"] = ["engaged"]
        merged_by_id[mr_id] = _merge_mr(merged_by_id.get(mr_id), row)
        engaged_added += 1

    mrs = list(merged_by_id.values())

    # Compute per-MR review stage. Only opened MRs where the user reviews
    # (formal or engaged) need a comment scan — merged/closed MRs and MRs
    # I authored skip the extra API call.
    my_username = (
        config["username"] or current_user(config).get("username", "")
    ).strip()
    to_scan = [m for m in mrs
               if m.get("state") == "opened" and m.get("role") in ("reviewer", "engaged")]
    scan_results = {}
    if to_scan:
        with ThreadPoolExecutor(max_workers=REVIEW_SCAN_WORKERS) as pool:
            futures = {pool.submit(_has_my_approval, m["mr_id"], my_username, config): m["mr_id"]
                       for m in to_scan}
            for fut in as_completed(futures):
                mr_id = futures[fut]
                scan_results[mr_id] = fut.result()
    cache_rows = []
    for m in mrs:
        stages = _compute_stages(m, scan_results.get(m["mr_id"], False))
        cache_rows.append({
            **{key: value for key, value in m.items() if key != "_self_roles"},
            "roles": json.dumps(m.get("roles") or [m["role"]]),
            "assignee_usernames": json.dumps(m.get("assignee_usernames") or []),
            "reviewer_usernames": json.dumps(m.get("reviewer_usernames") or []),
            "stages": json.dumps(stages),
            "labels": json.dumps(m.get("labels") or []),
            "synced_at": synced_at,
        })

    with conn:
        conn.execute("DELETE FROM gitlab_mrs_cache")
        conn.executemany(
            "INSERT INTO gitlab_mrs_cache (mr_id, project, title, state, url, role,"
            " roles, author, author_username, assignee_usernames, reviewer_usernames,"
            " source_branch, description, updated_at, labels, stages, synced_at)"
            " VALUES ("
            " :mr_id, :project, :title, :state, :url, :role, :roles, :author,"
            " :author_username, :assignee_usernames, :reviewer_usernames,"
            " :source_branch, :description, :updated_at, :labels, :stages, :synced_at)",
            cache_rows,
        )
    log.info(
        "gitlab sync: %d MRs (incl. %d referenced, %d engaged); scanned %d for approval",
        len(mrs), referenced_added, engaged_added, len(to_scan),
    )
    return len(mrs)
