from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    anthropic_base_url: str = ""  # optional gateway, e.g. an API gateway
    anthropic_model: str = "claude-sonnet-4-6"
    watson_user_name: str = "User"  # the owner; their own work is attributed to this
    people_email_domain: str = ""
    clickup_api_token: str = ""
    clickup_list_ids: str = ""
    # where Watson creates your own tasks; defaults to the first synced list
    clickup_create_list_id: str = ""
    gitlab_base_url: str = ""
    gitlab_token: str = ""
    gitlab_username: str = ""
    # how far back to scan GitLab activity (comments, approvals) when deciding
    # which MRs to keep tracked even after reassignment
    engagement_lookback_days: int = 14
    # Substring (case-insensitive) that identifies the "ready for QA" label
    # on your team's MRs. The MR-stage badge on the Today card tags any MR
    # carrying a matching label as "QA".
    gitlab_qa_label_substring: str = "ready for qa"
    watson_data_dir: str = "~/.watson"
    watson_backup_dir: str = "~/GoogleDrive/watson"
    digest_time: str = "09:30"
    digest_webhook_url: str = ""
    tz: str = "Asia/Kolkata"
    testing: bool = False

    # Google Calendar legacy import sources. Runtime OAuth credentials live in
    # Keychain; Settings imports these files only when explicitly requested.
    gcal_credentials_path: str = "~/.watson/gcal_credentials.json"
    gcal_token_path: str = ""  # defaults to data_dir/gcal_token.json

    # Flock (optional). We drive a headless Playwright Chromium against
    # web.flock.com using a dedicated persistent profile — Flock has no
    # user-level REST API for reading DMs / mentions. `python -m app.auth.flock`
    # opens a visible browser once so you can log in; after that the sync step
    # reuses the persisted cookies silently.
    flock_profile_dir: str = ""            # defaults to data_dir/chrome-profile-flock
    flock_url: str = "https://web.flock.com/"
    # Only include chats whose lastMessageTime falls within this many hours of "now".
    flock_lookback_hours: int = 24
    # The user's Flock display-name substring to match in Outgoing Webhook
    # message text (case-insensitive). Flock renders mentions as literal
    # "@Taylor S" in the text, so we substring-match on "@<flock_user_handle>".
    # Also unconditionally matches @all / @online. Empty disables webhook
    # filtering (accepts everything — noisy).
    flock_user_handle: str = ""

    @property
    def data_dir(self) -> Path:
        """Local home of the live SQLite db. Kept off cloud-synced folders to
        avoid mid-write corruption."""
        return Path(self.watson_data_dir).expanduser()

    @property
    def backup_dir(self) -> Path:
        """Cloud-synced (e.g. Google Drive) destination for the daily markdown
        journal and nightly db backups. Falls back to data_dir if unset."""
        target = self.watson_backup_dir.strip() or self.watson_data_dir
        return Path(target).expanduser()

    @property
    def clickup_list_id_list(self) -> list:
        return [x.strip() for x in self.clickup_list_ids.split(",") if x.strip()]

    @property
    def clickup_create_list(self) -> str:
        """List id for tasks Watson creates for you; falls back to first synced list."""
        if self.clickup_create_list_id.strip():
            return self.clickup_create_list_id.strip()
        ids = self.clickup_list_id_list
        return ids[0] if ids else ""

    @property
    def gcal_credentials_file(self) -> Path:
        return Path(self.gcal_credentials_path).expanduser()

    @property
    def gcal_token_file(self) -> Path:
        target = self.gcal_token_path.strip() or str(self.data_dir / "gcal_token.json")
        return Path(target).expanduser()

    @property
    def flock_profile_path(self) -> Path:
        target = self.flock_profile_dir.strip() or str(self.data_dir / "chrome-profile-flock")
        return Path(target).expanduser()


settings = Settings()
