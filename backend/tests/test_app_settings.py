from app.services import app_settings


def test_app_setting_roundtrips_json_values_and_deletes(conn):
    app_settings.set_value(conn, "profile.timezone", "Asia/Kolkata")
    app_settings.set_value(conn, "profile.compact_mode", True)
    app_settings.set_value(conn, "profile.refresh_seconds", 90)
    app_settings.set_value(conn, "profile.filters", ["mine", "waiting"])

    assert app_settings.get(conn, "profile.timezone") == "Asia/Kolkata"
    assert app_settings.get(conn, "profile.compact_mode") is True
    assert app_settings.get(conn, "profile.refresh_seconds") == 90
    assert app_settings.get(conn, "profile.filters") == ["mine", "waiting"]

    app_settings.delete(conn, "profile.timezone")
    assert app_settings.get(conn, "profile.timezone", "UTC") == "UTC"


def test_snapshot_returns_exactly_the_requested_prefix(conn):
    app_settings.set_value(conn, "integration.gitlab.base_url", "https://gitlab.example.com")
    app_settings.set_value(conn, "integration.gitlab.enabled", False)
    app_settings.set_value(conn, "integrationx.gitlab.base_url", "https://wrong.example.com")
    app_settings.set_value(conn, "profile.name", "Alex")

    assert app_settings.snapshot(conn, "integration.") == {
        "integration.gitlab.base_url": "https://gitlab.example.com",
        "integration.gitlab.enabled": False,
    }


def test_effective_nonsecret_observes_later_stored_values(conn):
    assert app_settings.effective_nonsecret("profile.timezone", "UTC") == "UTC"

    app_settings.set_value(conn, "profile.timezone", "Asia/Kolkata")
    assert app_settings.effective_nonsecret("profile.timezone", "UTC") == "Asia/Kolkata"

    app_settings.set_value(conn, "profile.timezone", "Europe/London")
    assert app_settings.effective_nonsecret("profile.timezone", "UTC") == "Europe/London"
