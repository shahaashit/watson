import requests
import pytest
from concurrent.futures import ThreadPoolExecutor


class FakeResponse:
    def __init__(
        self, status_code, *, headers=None, json_body=None,
        json_error=None, raise_error=None,
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self._json_body = json_body or {}
        self._json_error = json_error
        self._raise_error = raise_error
        self.close_count = 0

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._json_body

    def raise_for_status(self):
        if self._raise_error:
            raise self._raise_error
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def close(self):
        self.close_count += 1


def _configured(monkeypatch, clickup_client, token="test-token"):
    monkeypatch.setattr(
        clickup_client,
        "clickup_config",
        lambda: {"token": token, "list_ids": (), "create_list_id": ""},
    )


def test_retry_after_header_controls_delay(monkeypatch):
    from app.services import clickup_client

    _configured(monkeypatch, clickup_client)
    waits = []
    responses = iter([
        FakeResponse(429, headers={"Retry-After": "4"}),
        FakeResponse(200, json_body={"id": "86abc1234", "name": "Task"}),
    ])
    monkeypatch.setattr(clickup_client.requests, "get", lambda *a, **k: next(responses))
    task = clickup_client.get_task_resilient(
        "86abc1234", sleeper=waits.append, random_value=lambda: 0
    )

    assert task["name"] == "Task"
    assert waits == [4.0]


@pytest.mark.parametrize(
    ("header", "wall_clock", "expected_wait"),
    [
        ("Wed, 21 Oct 2015 07:28:00 GMT", 1445412470.0, 10.0),
        ("Wed, 21 Oct 2015 07:28:00 GMT", 1445412490.0, 0.0),
        ("not-a-date", 1445412470.0, 1.25),
    ],
)
def test_retry_after_supports_http_dates_and_safe_fallbacks(
    monkeypatch, header, wall_clock, expected_wait
):
    from app.services import clickup_client

    _configured(monkeypatch, clickup_client)
    waits = []
    responses = iter([
        FakeResponse(429, headers={"Retry-After": header}),
        FakeResponse(200, json_body={"id": "86abc1234"}),
    ])
    monkeypatch.setattr(clickup_client.requests, "get", lambda *a, **k: next(responses))

    assert clickup_client.get_task_resilient(
        "86abc1234",
        sleeper=waits.append,
        random_value=lambda: 0.25,
        wall_clock=lambda: wall_clock,
    ) == {"id": "86abc1234"}
    assert waits == [expected_wait]


def test_timeout_retries_with_capped_exponential_jitter(monkeypatch):
    from app.services import clickup_client

    _configured(monkeypatch, clickup_client)
    waits = []
    responses = iter([
        requests.Timeout("temporary"),
        FakeResponse(200, json_body={"id": "86abc1234", "name": "Task"}),
    ])

    def request(*_args, **_kwargs):
        item = next(responses)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(clickup_client.requests, "get", request)
    task = clickup_client.get_task_resilient(
        "86abc1234",
        policy=clickup_client.RetryPolicy(base_delay=2, max_delay=2),
        sleeper=waits.append,
        random_value=lambda: 0.25,
    )

    assert task["id"] == "86abc1234"
    assert waits == [2.25]


def test_retries_keep_one_immutable_credential_snapshot(monkeypatch):
    from app.services import clickup_client

    config = {"token": "first-token", "list_ids": (), "create_list_id": ""}
    monkeypatch.setattr(clickup_client, "clickup_config", lambda: config)
    headers = []
    responses = iter([
        FakeResponse(429),
        FakeResponse(200, json_body={"id": "86abc1234"}),
    ])

    def request(*_args, **kwargs):
        headers.append(kwargs["headers"]["Authorization"])
        config["token"] = "rotated-token"
        return next(responses)

    monkeypatch.setattr(clickup_client.requests, "get", request)

    assert clickup_client.get_task_resilient(
        "86abc1234", sleeper=lambda _delay: None, random_value=lambda: 0
    ) == {"id": "86abc1234"}
    assert headers == ["first-token", "first-token"]


def test_bad_request_is_not_retried(monkeypatch):
    from app.services import clickup_client

    _configured(monkeypatch, clickup_client)
    calls = []
    waits = []
    monkeypatch.setattr(
        clickup_client.requests,
        "get",
        lambda *args, **kwargs: calls.append((args, kwargs)) or FakeResponse(400),
    )

    with pytest.raises(requests.HTTPError):
        clickup_client.get_task_resilient("86abc1234", sleeper=waits.append)

    assert len(calls) == 1
    assert waits == []


def test_circuit_breaker_opens_after_failures_and_skips_requests(monkeypatch):
    from app.services import clickup_client

    _configured(monkeypatch, clickup_client)
    breaker = clickup_client.CircuitBreaker(failure_threshold=2, cooldown_seconds=120)
    monkeypatch.setattr(clickup_client, "_TASK_CIRCUIT_BREAKER", breaker)
    monkeypatch.setattr(clickup_client.time, "monotonic", lambda: 100.0)
    calls = []
    waits = []

    def timeout(*_args, **_kwargs):
        calls.append(1)
        raise requests.Timeout("unavailable")

    monkeypatch.setattr(clickup_client.requests, "get", timeout)
    policy = clickup_client.RetryPolicy(max_attempts=1)
    with pytest.raises(requests.Timeout):
        clickup_client.get_task_resilient("one", policy=policy, sleeper=waits.append)
    with pytest.raises(requests.Timeout):
        clickup_client.get_task_resilient("two", policy=policy, sleeper=waits.append)
    with pytest.raises(clickup_client.CircuitOpenError):
        clickup_client.get_task_resilient("three", policy=policy, sleeper=waits.append)

    assert len(calls) == 2
    assert waits == []


def test_half_open_breaker_allows_one_concurrent_probe_and_ignores_late_failures(monkeypatch):
    from app.services import clickup_client

    now = [100.0]
    monkeypatch.setattr(clickup_client.time, "monotonic", lambda: now[0])
    breaker = clickup_client.CircuitBreaker(failure_threshold=2, cooldown_seconds=10)
    key = b"credential"
    first = breaker.permit(key)
    second = breaker.permit(key)
    breaker.record_failure(first)
    breaker.record_failure(second)
    opened_at = breaker._opened_at

    now[0] += 10
    with ThreadPoolExecutor(max_workers=4) as pool:
        permits = list(pool.map(lambda _ignored: breaker.permit(key), range(4)))

    probes = [permit for permit in permits if permit is not None]
    assert len(probes) == 1
    assert breaker._state == "half_open"
    breaker.record_failure(second)  # old lease must neither reopen nor extend cooldown
    assert breaker._opened_at == opened_at
    breaker.record_success(probes[0])
    assert breaker._state == "closed"


def test_credential_rotation_invalidates_late_old_epoch_results(monkeypatch):
    from app.services import clickup_client

    monkeypatch.setattr(clickup_client.time, "monotonic", lambda: 100.0)
    breaker = clickup_client.CircuitBreaker(failure_threshold=1, cooldown_seconds=120)
    old = breaker.permit(b"old")
    fresh = breaker.permit(b"new")
    breaker.record_failure(old)

    assert breaker._state == "closed"
    breaker.record_failure(fresh)
    assert breaker._state == "open"


def test_new_credential_generation_is_not_blocked_by_old_breaker(monkeypatch):
    from app.services import clickup_client

    breaker = clickup_client.CircuitBreaker(failure_threshold=1, cooldown_seconds=120)
    monkeypatch.setattr(clickup_client, "_TASK_CIRCUIT_BREAKER", breaker)
    monkeypatch.setattr(clickup_client.time, "monotonic", lambda: 100.0)
    tokens = iter(["old", "new"])
    monkeypatch.setattr(
        clickup_client,
        "clickup_config",
        lambda: {"token": next(tokens), "list_ids": (), "create_list_id": ""},
    )
    responses = iter([requests.Timeout("old credential unavailable"), FakeResponse(200, json_body={"id": "two"})])

    def request(*_args, **_kwargs):
        item = next(responses)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(clickup_client.requests, "get", request)
    with pytest.raises(requests.Timeout):
        clickup_client.get_task_resilient("one", policy=clickup_client.RetryPolicy(max_attempts=1))

    assert clickup_client.get_task_resilient("two", policy=clickup_client.RetryPolicy(max_attempts=1)) == {"id": "two"}


def test_resilient_fetch_closes_success_and_retry_responses(monkeypatch):
    from app.services import clickup_client

    _configured(monkeypatch, clickup_client)
    retry = FakeResponse(429)
    success = FakeResponse(200, json_body={"id": "task"})
    responses = iter([retry, success])
    monkeypatch.setattr(clickup_client.requests, "get", lambda *a, **k: next(responses))

    assert clickup_client.get_task_resilient(
        "task", sleeper=lambda _delay: None, random_value=lambda: 0
    ) == {"id": "task"}
    assert (retry.close_count, success.close_count) == (1, 1)


@pytest.mark.parametrize(
    "response, expected_error",
    [
        (FakeResponse(400), requests.HTTPError),
        (FakeResponse(200, json_error=ValueError("bad json")), ValueError),
        (FakeResponse(200, raise_error=RuntimeError("bad status")), RuntimeError),
    ],
)
def test_resilient_fetch_closes_response_after_nonretry_and_parser_errors(
    monkeypatch, response, expected_error
):
    from app.services import clickup_client

    _configured(monkeypatch, clickup_client)
    monkeypatch.setattr(clickup_client.requests, "get", lambda *a, **k: response)

    with pytest.raises(expected_error):
        clickup_client.get_task_resilient("task")
    assert response.close_count == 1


@pytest.mark.parametrize(
    "response, expected_error",
    [
        (FakeResponse(400), requests.HTTPError),
        (FakeResponse(200, json_error=ValueError("bad json")), ValueError),
    ],
)
def test_half_open_neutral_errors_release_the_only_probe(monkeypatch, response, expected_error):
    from app.services import clickup_client

    now = [100.0]
    monkeypatch.setattr(clickup_client.time, "monotonic", lambda: now[0])
    _configured(monkeypatch, clickup_client)
    breaker = clickup_client.CircuitBreaker(failure_threshold=1, cooldown_seconds=0)
    monkeypatch.setattr(clickup_client, "_TASK_CIRCUIT_BREAKER", breaker)
    opening = breaker.permit(clickup_client._credential_key(clickup_client.clickup_config()))
    breaker.record_failure(opening)
    monkeypatch.setattr(clickup_client.requests, "get", lambda *a, **k: response)

    with pytest.raises(expected_error):
        clickup_client.get_task_resilient("task")

    assert breaker.permit(clickup_client._credential_key(clickup_client.clickup_config())) is not None
    assert response.close_count == 1


def test_unexpected_request_error_releases_the_acquired_permit(monkeypatch):
    from app.services import clickup_client

    now = [100.0]
    monkeypatch.setattr(clickup_client.time, "monotonic", lambda: now[0])
    _configured(monkeypatch, clickup_client)
    breaker = clickup_client.CircuitBreaker(failure_threshold=1, cooldown_seconds=0)
    monkeypatch.setattr(clickup_client, "_TASK_CIRCUIT_BREAKER", breaker)
    key = clickup_client._credential_key(clickup_client.clickup_config())
    opening = breaker.permit(key)
    breaker.record_failure(opening)
    def unexpected(*_args, **_kwargs):
        raise requests.RequestException("unexpected")

    monkeypatch.setattr(clickup_client.requests, "get", unexpected)

    with pytest.raises(requests.RequestException):
        clickup_client.get_task_resilient("task")

    assert breaker.permit(key) is not None


def test_failed_refresh_keeps_cached_row(conn, monkeypatch):
    from app.services import clickup_client

    _configured(monkeypatch, clickup_client)
    conn.execute(
        "INSERT INTO clickup_tasks_cache (task_id,name,synced_at) "
        "VALUES ('86abc1234','Cached','old')"
    )
    conn.commit()
    monkeypatch.setattr(
        clickup_client,
        "get_task_resilient",
        lambda task_id, **_kwargs: (_ for _ in ()).throw(TimeoutError("down")),
    )

    result = clickup_client.refresh_exact_tasks(conn, ["86abc1234"])

    assert result == {"updated": 0, "failed": 1, "skipped": 0}
    assert conn.execute(
        "SELECT name FROM clickup_tasks_cache WHERE task_id='86abc1234'"
    ).fetchone()["name"] == "Cached"


def test_restricted_task_is_reported_separately_from_connection_failure(conn, monkeypatch):
    from app.services import clickup_client, sync_pipeline, user_meta
    _configured(monkeypatch, clickup_client)
    def response(url, **kwargs):
        if url.endswith('/restricted'):
            return FakeResponse(401, json_body={'ECODE': 'OAUTH_027'})
        return FakeResponse(200, json_body={'id': 'good', 'name': 'Accessible', 'status': {}})
    monkeypatch.setattr(clickup_client.requests, 'get', response)
    result = clickup_client.refresh_exact_tasks(conn, ['restricted', 'good'])
    assert result == {'updated': 1, 'failed': 1, 'skipped': 0, 'restricted': 1}
    sync_pipeline._mark_source_result(conn, 'clickup', 'clickup_exact', result)
    health = next(s for s in user_meta.integration_health(conn) if s['source'] == 'clickup')
    assert '1 task refreshed' in health['message']
    assert '1 linked task requires workspace access' in health['message']
    assert health['last_success_at']


def test_exact_refresh_deduplicates_and_upserts_each_success(conn, monkeypatch):
    from app.services import clickup_client

    _configured(monkeypatch, clickup_client)
    fetched = []

    def get_task(task_id, **_kwargs):
        fetched.append(task_id)
        return {
            "id": task_id,
            "name": f"Fresh {task_id}",
            "status": {"status": "open", "type": "open"},
            "assignees": [],
        }

    monkeypatch.setattr(clickup_client, "get_task_resilient", get_task)
    result = clickup_client.refresh_exact_tasks(
        conn, ["b-task", "a-task", "b-task", "", None]
    )

    assert result == {"updated": 2, "failed": 0, "skipped": 0}
    assert fetched == ["b-task", "a-task"]
    assert [tuple(row) for row in conn.execute(
        "SELECT task_id, name FROM clickup_tasks_cache ORDER BY task_id"
    )] == [("a-task", "Fresh a-task"), ("b-task", "Fresh b-task")]


def test_refresh_counts_bad_shape_without_aborting_good_upsert(conn, monkeypatch):
    from app.services import clickup_client

    _configured(monkeypatch, clickup_client)
    monkeypatch.setattr(
        clickup_client,
        "get_task_resilient",
        lambda task_id, **_kwargs: ({"name": "invalid"} if task_id == "bad" else {
            "id": task_id, "name": "Good", "status": {}, "assignees": [],
        }),
    )

    result = clickup_client.refresh_exact_tasks(conn, ["bad", "good"])

    assert result == {"updated": 1, "failed": 1, "skipped": 0}
    assert conn.execute("SELECT name FROM clickup_tasks_cache WHERE task_id='good'").fetchone()["name"] == "Good"


class _FaultingConnection:
    def __init__(self, conn, *, fail_execute_at=None, fail_commit=False):
        self._conn = conn
        self._fail_execute_at = fail_execute_at
        self._fail_commit = fail_commit
        self._writes = 0

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *args, **kwargs):
        if sql.startswith("INSERT INTO clickup_tasks_cache"):
            self._writes += 1
            if self._writes == self._fail_execute_at:
                raise RuntimeError("db write failed")
        return self._conn.execute(sql, *args, **kwargs)

    def commit(self):
        if self._fail_commit:
            raise RuntimeError("db commit failed")
        return self._conn.commit()


@pytest.mark.parametrize("fault", ["second_execute", "commit"])
def test_refresh_rolls_back_all_rows_when_db_phase_fails(conn, monkeypatch, fault):
    from app.services import clickup_client

    _configured(monkeypatch, clickup_client)
    monkeypatch.setattr(
        clickup_client,
        "get_task_resilient",
        lambda task_id, **_kwargs: {"id": task_id, "name": task_id, "status": {}, "assignees": []},
    )
    faulty = _FaultingConnection(
        conn,
        fail_execute_at=2 if fault == "second_execute" else None,
        fail_commit=fault == "commit",
    )

    result = clickup_client.refresh_exact_tasks(faulty, ["one", "two"])

    assert result == {"updated": 0, "failed": 2, "skipped": 0}
    assert conn.execute("SELECT COUNT(*) FROM clickup_tasks_cache").fetchone()[0] == 0
    assert conn.in_transaction is False


def test_refresh_uses_savepoint_without_rolling_back_outer_transaction(conn, monkeypatch):
    from app.services import clickup_client

    _configured(monkeypatch, clickup_client)
    conn.execute("BEGIN")
    conn.execute(
        "INSERT INTO clickup_tasks_cache (task_id, name, synced_at) VALUES ('outer', 'Outer', 'now')"
    )
    monkeypatch.setattr(
        clickup_client,
        "get_task_resilient",
        lambda task_id, **_kwargs: {"id": task_id, "name": task_id, "status": {}, "assignees": []},
    )
    result = clickup_client.refresh_exact_tasks(
        _FaultingConnection(conn, fail_execute_at=2), ["one", "two"]
    )

    assert result == {"updated": 0, "failed": 2, "skipped": 0}
    assert conn.execute("SELECT task_id FROM clickup_tasks_cache ORDER BY task_id").fetchall()[0][0] == "outer"
    assert conn.in_transaction is True
    conn.rollback()


def test_manual_clickup_import_uses_resilient_fetch(client, monkeypatch):
    from app.services import clickup_client

    monkeypatch.setattr(
        clickup_client,
        "get_task_resilient",
        lambda task_id: {
            "id": task_id,
            "name": "Resilient import",
            "url": f"https://app.clickup.com/t/{task_id}",
            "status": {"status": "open", "type": "open"},
            "assignees": [],
        },
    )

    response = client.post(
        "/api/work-import", json={"url": "https://app.clickup.com/t/86abc1234"}
    )

    assert response.status_code == 201
    assert response.json()["work_item"]["title"] == "Resilient import"
