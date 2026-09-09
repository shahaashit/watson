"""Tests for the shared user_meta helpers — kv store + Google URL rewriter."""
from app.services import user_meta


def test_get_set_meta_roundtrip(conn):
    assert user_meta.get_meta(conn, "missing") == ""
    assert user_meta.get_meta(conn, "missing", "fallback") == "fallback"
    user_meta.set_meta(conn, "greeting", "hello")
    assert user_meta.get_meta(conn, "greeting") == "hello"
    # Update should replace, not append.
    user_meta.set_meta(conn, "greeting", "hi")
    assert user_meta.get_meta(conn, "greeting") == "hi"


def test_google_user_email_convenience_helpers(conn):
    assert user_meta.google_user_email(conn) == ""
    user_meta.set_google_user_email(conn, "taylor@example.com")
    assert user_meta.google_user_email(conn) == "taylor@example.com"


def test_set_google_user_email_no_op_for_empty(conn):
    user_meta.set_google_user_email(conn, "")
    assert user_meta.google_user_email(conn) == ""


# ─── with_authuser_if_google ─────────────────────────────────────────────

def test_authuser_appended_on_calendar_url():
    url = "https://www.google.com/calendar/event?eid=abc"
    out = user_meta.with_authuser_if_google(url, "me@example.com")
    assert "authuser=me%40example.com" in out


def test_authuser_appended_on_meet_url():
    url = "https://meet.google.com/abc-defg-hij"
    out = user_meta.with_authuser_if_google(url, "me@example.com")
    assert "authuser=me%40example.com" in out
    assert out.startswith("https://meet.google.com/abc-defg-hij")


def test_authuser_appended_on_docs_url():
    """Future Docs / Drive / Mail URLs are covered by the same domain match."""
    for url in [
        "https://docs.google.com/document/d/xxx/edit",
        "https://drive.google.com/file/d/xxx/view",
        "https://mail.google.com/mail/u/0/#inbox",
    ]:
        out = user_meta.with_authuser_if_google(url, "me@example.com")
        assert "authuser=me%40example.com" in out, f"expected authuser on {url}"


def test_authuser_replaces_existing_value():
    url = "https://meet.google.com/abc?authuser=stale@example.com"
    out = user_meta.with_authuser_if_google(url, "fresh@example.com")
    assert "authuser=fresh%40example.com" in out
    assert "stale%40example.com" not in out


def test_non_google_url_passes_through():
    """Zoom, Teams, arbitrary URLs must not be touched — they'd break."""
    for url in [
        "https://zoom.us/j/12345",
        "https://teams.microsoft.com/l/meetup-join/abc",
        "https://gitlab.example.com/foo/bar",
        "https://example.com/",
    ]:
        assert user_meta.with_authuser_if_google(url, "me@example.com") == url


def test_empty_email_or_url_is_noop():
    assert user_meta.with_authuser_if_google("", "me@example.com") == ""
    assert user_meta.with_authuser_if_google("https://meet.google.com/abc", "") == "https://meet.google.com/abc"


def test_domain_match_is_strict_not_substring():
    """`googleusercontent.com` and `not-google.com` must NOT match."""
    for url in [
        "https://phishing.notgoogle.com/",
        "https://googleusercontent.com/",  # image CDN, doesn't need authuser
        "https://google.evil.com/",
    ]:
        assert user_meta.with_authuser_if_google(url, "me@example.com") == url
