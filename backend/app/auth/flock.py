"""One-time Flock login into a dedicated Playwright browser profile.

Run: python -m app.auth.flock

Opens a visible Chromium window pointing at web.flock.com. Sign in, close
the window. The resulting cookies / IndexedDB persist under
settings.flock_profile_path so subsequent headless sync() runs reuse the
session silently.
"""
from ..services import flock_client


if __name__ == "__main__":
    flock_client.run_auth_flow()
