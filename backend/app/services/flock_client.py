"""Flock inbox cache refresh.

Flock has no user-level REST API for reading a user's own DMs / @-mentions —
their web app is the only surface with that data. We drive a headless Chromium
via Playwright against web.flock.com using a dedicated persistent profile,
walk the React fiber on each sidebar chat row to extract the `contact` object
(the same technique validated by the sibling `Flock Extension` project's
architecture doc), and cache the interesting rows: those with @-mentions of
the user, and unread 1:1 DMs.

The one-time login is performed by `python -m app.auth.flock`; this module's
`sync` only reads the persisted profile. If the login has expired, Playwright
returns without the sidebar loading and `sync` gets zero rows — the user
re-runs the auth entrypoint.
"""
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from urllib.parse import quote

from ..config import settings
from ..models import now_iso
from . import app_settings, external_errors

log = logging.getLogger("watson.flock")


class FlockRefreshError(RuntimeError):
    """The deep-read failed after preserving the last usable cache snapshot."""

# Flock's web app runs a client-side browser-sniff and hard-blocks anything
# that looks like an automated Chromium ("Your browser is not supported").
# We escape by:
#   1. Driving the user's real Google Chrome install (channel="chrome") rather
#      than Playwright's bundled Chromium — the bundled build's UA carries
#      "HeadlessChrome" which Flock's regex almost certainly matches.
#   2. Forcing a plain Chrome UA as belt-and-suspenders in case the channel
#      flag is unavailable on this system.
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
CHROMIUM_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
]

# Flock's web app is a client-routed SPA served from /. There is no /app/
# path (verified: S3 returns 404 for that key). Authenticated users load /
# and the SPA renders the sidebar; unauthenticated users get the marketing
# page. Whether the SPA sees a session depends on Chrome finding decryptable
# cookies in the profile — which requires the SAME Chrome build to have
# written them (see channel="chrome" comment above).

# Chrome writes these files inside the profile dir while running. If a prior
# launch died without a clean shutdown, they linger and the next launch aborts
# with "Failed to create SingletonLock: File exists (17)". Safe to delete when
# no Chrome instance is currently using the profile.
_SINGLETON_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")
_COOKIE_DATABASES = (
    Path("Default/Network/Cookies"),
    Path("Default/Cookies"),
    Path("Cookies"),
)
_CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def flock_config() -> dict:
    """Resolve current Flock profile settings for each browser operation."""
    if app_settings.integration_disabled("flock"):
        return MappingProxyType({
            "disabled": True,
            "profile_path": Path(),
            "user_handle": "",
            "url": "",
            "lookback_hours": settings.flock_lookback_hours,
        })
    try:
        profile_dir = app_settings.effective_nonsecret(
            "integration.flock.profile_dir", settings.flock_profile_dir
        )
        user_handle = app_settings.effective_nonsecret(
            "integration.flock.user_handle", settings.flock_user_handle
        )
    except Exception:
        return MappingProxyType({
            "disabled": True,
            "profile_path": Path(),
            "user_handle": "",
            "url": "",
            "lookback_hours": settings.flock_lookback_hours,
        })
    profile_path = (
        Path(str(profile_dir)).expanduser()
        if str(profile_dir or "").strip()
        else settings.data_dir / "chrome-profile-flock"
    )
    return MappingProxyType({
        "disabled": False,
        "profile_path": profile_path,
        "user_handle": str(user_handle or ""),
        "url": settings.flock_url,
        "lookback_hours": settings.flock_lookback_hours,
    })


def probe_authenticated_session(
    profile_dir: Path | str, *, timeout_seconds: float = 2.0
) -> bool:
    """Read-only, local proof that the Chrome profile has a Flock session.

    A random profile file is not authentication.  Open the first expected
    Chrome Cookies database in SQLite read-only mode and require the exact
    non-empty ``flock-login`` cookie on Flock's canonical cookie domain. A
    zero expiry is a current browser-session cookie; persistent cookies must
    be later than the current Chrome timestamp (microseconds since 1601).
    The busy timeout is hard-capped so a Chrome lock cannot stall Settings or
    the sync gate.
    """
    profile = Path(profile_dir).expanduser()
    cookie_db = next(
        (profile / relative for relative in _COOKIE_DATABASES if (profile / relative).is_file()),
        None,
    )
    if cookie_db is None:
        return False
    timeout = min(5.0, max(0.05, float(timeout_seconds)))
    uri = f"file:{quote(str(cookie_db.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=timeout)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        now_chrome_micros = int(
            (datetime.now(timezone.utc) - _CHROME_EPOCH).total_seconds()
            * 1_000_000
        )
        row = connection.execute(
            "SELECT 1 FROM cookies "
            "WHERE name = 'flock-login' "
            "AND host_key IN ('.flock.com', 'flock.com') "
            "AND length(encrypted_value) > 0 "
            "AND (expires_utc = 0 OR expires_utc > ?) LIMIT 1",
            (now_chrome_micros,),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def _clean_stale_singleton_files(profile_dir):
    for name in _SINGLETON_FILES:
        p = profile_dir / name
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            external_errors.log_failure(
                log, f"remove stale Flock singleton {name}", "flock", exc
            )


# Fiber-walk JS. Selectors and prop paths mirror the sibling Flock Extension
# architecture doc §5.1 — they are the durable contract (Flock's dev has
# stalled; hashed classnames may drift but React prop names are stable).
FIBER_WALK_JS = r"""
() => {
  const rows = document.querySelectorAll('#active-chats [role="button"][tabindex="0"]');
  const out = [];
  rows.forEach((el) => {
    const key = Object.keys(el).find((k) =>
      k.indexOf('__reactFiber') === 0 || k.indexOf('__reactInternalInstance') === 0);
    if (!key) return;
    let n = el[key];
    while (n) {
      const p = n.memoizedProps;
      if (p && p.contact && p.contact.jid) {
        const c = p.contact;
        out.push({
          jid: c.jid,
          name: c.name || c.chatName || '',
          type: c.type || '',
          isGroup: c.type === 'group',
          hasMention: !!c.hasMention,
          unreadCount: c.unreadCount || 0,
          lastMessageTime: c.lastMessageTime || null,
          isMuted: !!c.isMuted,
          notifyOn: c.notifyOn || '',
          bucket: p.bucket || null,
        });
        return;
      }
      n = n.return;
    }
  });
  return JSON.stringify(out);
}
"""


def configured(config=None) -> bool:
    """True only for a non-disabled profile with authenticated Flock cookies."""
    config = config or flock_config()
    if config["disabled"]:
        return False
    try:
        return probe_authenticated_session(
            config["profile_path"], timeout_seconds=2.0
        )
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
        return False


def _to_iso(ms):
    """Flock's lastMessageTime is ms-since-epoch (or null). Convert to ISO
    string in the configured timezone; return None on null/invalid."""
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _relevant(chat: dict) -> bool:
    """Keep only chats worth surfacing on the morning brief:
      - anyone who @-mentioned me and I haven't read yet (sidebar hasMention), OR
      - a 1:1 DM with any unread.
    Muted noise channels without any mention still drop. Deep-reading a
    chat's message pane would fire Flock's read-receipt path and hide the
    unread badge in the user's real Flock client — so we intentionally
    surface only sidebar-level signal here.
    """
    if chat.get("hasMention"):
        return True
    if chat.get("type") == "buddy" and (chat.get("unreadCount") or 0) > 0:
        return True
    return False


def _within_lookback(chat: dict, config=None) -> bool:
    """Only include chats whose last activity is within the lookback window.

    Older mentions have already had a chance to appear in previous briefs;
    surfacing them again is noise.
    """
    lm = chat.get("lastMessageTime")
    if not lm:
        return False
    config = config or flock_config()
    cutoff_ms = int(
        (
            datetime.now()
            - timedelta(hours=config["lookback_hours"])
        ).timestamp()
        * 1000
    )
    try:
        return int(lm) >= cutoff_ms
    except (TypeError, ValueError):
        return False


def _flock_deep_link(jid: str) -> str:
    url = flock_config()["url"]
    if not jid:
        return url
    return url.rstrip("/") + "#/chat/" + jid


def _fetch_inbox(profile_dir, url: str, timeout_ms: int = 30_000):
    """Drive Playwright in a subprocess and return the parsed fiber-walk output.

    Why subprocess: `sync_playwright()` deadlocks when invoked from uvicorn's
    threadpool worker — the async infrastructure around that thread conflicts
    with Playwright's greenlet-based sync driver. A dedicated Python subprocess
    has a clean event loop, and `subprocess.run(timeout=)` gives us a hard
    upper bound so nothing can hang forever.

    Kept as its own function so tests can monkey-patch it — Playwright is a
    heavy dep and the sync path needs to be testable without a browser.

    Diagnostics on timeout: the worker dumps a screenshot to <data_dir>/
    flock-timeout.png plus the URL Chromium landed at. Worker stderr is
    always logged.
    """
    import subprocess
    import sys as _sys

    from ..config import settings as _settings

    # Sidebar-only walk: Chrome startup (~2-4s) + navigation (~1-2s) +
    # hydration wait (~1-3s) + fiber walk (~200ms). Well under 30s in practice.
    # Keep 60s headroom for cold Chrome starts / login-expired timeouts.
    hard_timeout_s = max(60, int(timeout_ms / 1000) + 30)

    base_cmd = [
        _sys.executable, "-m", "app.services.flock_worker",
        str(profile_dir), url, str(timeout_ms), str(_settings.data_dir),
    ]
    # Under macOS launchd, background agents run in a restricted session
    # context where Chrome / Playwright's Node driver can't spawn normally
    # (no access to Keychain, WindowServer, etc.). `launchctl asuser <uid>`
    # promotes the child into the console user's interactive Aqua session.
    # From a terminal-launched Watson it's a no-op — the process is already
    # in that session — so this is safe to apply unconditionally on macOS.
    cmd = _wrap_for_gui_session(base_cmd)
    log.info("flock: spawning worker (hard timeout %ss)", hard_timeout_s)
    # Scrub the child's env to a minimal safe set. Under launchd, the parent
    # inherits some environment bits that can make Playwright's Node driver
    # spawn hang (observed empirically: the same subprocess.run call runs in
    # ~4s from a plain shell, but hangs indefinitely under uvicorn-in-launchd).
    child_env = _minimal_env()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=hard_timeout_s,
            cwd=str(_settings_backend_dir()),
            # Detach the child so signals / TTY state don't propagate from
            # Watson's uvicorn process into Chrome.
            start_new_session=True,
            # Sever stdin — if Node's driver ever tries to read from it under
            # some code path, it would block forever on our closed pipe.
            stdin=subprocess.DEVNULL,
            env=child_env,
        )
    except subprocess.TimeoutExpired as exc:
        log.warning("flock worker timed out after %ss; killing — cache preserved", hard_timeout_s)
        # Kill any leftover Chrome children so they don't leak.
        _kill_stray_chrome(profile_dir)
        # None (not []) means "fetch failed" — sync() will keep the existing
        # cache rather than blanking it and leaving the user with an empty
        # section for the next hour until the scheduler tries again.
        return None

    if proc.returncode != 0:
        log.warning("flock worker exited %s — cache preserved", proc.returncode)
        return None

    stdout = proc.stdout.strip()
    if not stdout:
        # Sidebar didn't render (login expired, layout drift, headless-detection…).
        # Preserve the existing cache so the user's Today section isn't wiped
        # every time Flock hiccups.
        log.warning("flock worker returned empty stdout (sidebar not visible) — cache preserved")
        return None
    try:
        return json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        log.warning("flock worker returned non-JSON stdout — cache preserved")
        return None


def _settings_backend_dir():
    """Return the backend/ dir (one above app/) so subprocess -m resolves."""
    return Path(__file__).resolve().parents[2]


def _wrap_for_gui_session(cmd):
    """Prepend `launchctl asuser <uid>` on macOS so the child inherits the
    console user's interactive Aqua session (with full GUI service access).

    Verified empirically: without this wrap, Playwright's Node driver spawn
    hangs 45s+ under launchd; with it, the whole sync completes in ~5s.
    """
    import sys as _sys
    if _sys.platform != "darwin":
        return cmd
    # `id -u` = console user's uid; same as os.getuid() when Watson is running
    # as the user (which the plist guarantees).
    uid = str(os.getuid())
    return ["/bin/launchctl", "asuser", uid] + cmd


def _minimal_env():
    """Env for the Playwright subprocess. Copies only the variables its Node
    driver + Chromium actually need, dropping everything else (async
    frameworks / uvicorn / launchd bits that were experimentally observed to
    make the child hang on startup)."""
    keep = (
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TZ",
        "TMPDIR", "TEMP", "TMP",
        # Playwright / Chromium
        "DISPLAY", "XAUTHORITY",
        "PLAYWRIGHT_BROWSERS_PATH", "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD",
        # Our own opt-in for interactive debugging
        "FLOCK_HEADLESS",
        # macOS Keychain (Chrome needs it to decrypt persisted cookies)
        "SECURITYSESSIONID",
    )
    return {k: v for k, v in os.environ.items() if k in keep}


def _kill_stray_chrome(profile_dir):
    """After a worker timeout, any Chrome child that hasn't already died can
    leak. macOS-flavored pkill on the profile-dir path is the reliable filter."""
    import subprocess
    subprocess.run(["pkill", "-9", "-f", str(profile_dir)], check=False)


def _fetch_inbox_LEGACY(profile_dir, url: str, timeout_ms: int = 30_000):
    """LEGACY in-process driver — retained for reference / non-uvicorn callers.
    Do not use from within FastAPI request handling (see subprocess docstring
    above for the deadlock reason). Callable from a plain Python script."""
    from playwright.sync_api import sync_playwright

    from ..config import settings as _settings

    # FLOCK_HEADLESS=false lets the user run sync in a visible window for
    # debugging without editing code. Default is headless (production path).
    headless = os.environ.get("FLOCK_HEADLESS", "true").lower() != "false"

    _clean_stale_singleton_files(profile_dir)

    with sync_playwright() as pw:
        # channel="chrome" launches the user's installed Google Chrome
        # (/Applications/Google Chrome.app on macOS) instead of Playwright's
        # bundled Chromium — same profile dir, real Chrome binary. Fallback
        # to plain chromium if the channel isn't available.
        try:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel="chrome",
                headless=headless,
                args=CHROMIUM_ARGS,
                user_agent=CHROME_UA,
                viewport={"width": 1440, "height": 900},
            )
        except Exception as exc:  # noqa: BLE001
            external_errors.log_failure(log, "Chrome channel launch", "flock", exc)
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=headless,
                args=CHROMIUM_ARGS,
                user_agent=CHROME_UA,
                viewport={"width": 1440, "height": 900},
            )
        # Aggressive default so no single Playwright op can hang forever —
        # every wait_* inherits this unless we pass an explicit timeout.
        ctx.set_default_timeout(timeout_ms)
        try:
            page = ctx.new_page()
            log.info("flock: navigating to %s", url)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            log.info("flock: page loaded at %s, waiting for sidebar", page.url)
            # NOTE: we deliberately do NOT wait_for_load_state("networkidle").
            # Flock's SPA holds persistent WebSockets / long polls; the network
            # is never idle, and that wait hangs until timeout every time.
            # wait_for_selector alone is the right marker — it hard-caps at
            # timeout_ms and fires the moment the sidebar renders.
            try:
                page.wait_for_selector("#active-chats", timeout=timeout_ms)
            except Exception as exc:
                # Login expired, headless-detection page, or Flock changed
                # layout — dump state and return empty rather than raise.
                shot = _settings.data_dir / "flock-timeout.png"
                try:
                    page.screenshot(path=str(shot), full_page=True)
                    log.info(
                        "flock sidebar not visible; screenshot captured at %s", shot
                    )
                except Exception as shot_err:  # noqa: BLE001
                    external_errors.log_failure(
                        log, "Flock sidebar screenshot", "flock", shot_err
                    )
                return []
            log.info("flock: sidebar visible, walking fiber")
            # Small settle for React to finish attaching props to rows.
            page.wait_for_timeout(1500)
            raw = page.evaluate(FIBER_WALK_JS)
            log.info("flock: fiber walk complete")
        finally:
            ctx.close()

    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        log.warning("flock fiber walk returned non-JSON")
        return []


def sync(conn) -> int:
    """Refresh flock_mentions_cache. Full-replace semantics — no history kept.

    Returns the number of *relevant* chats stored. A relevant chat is one
    with a @-mention of the user OR an unread 1:1 DM, within the lookback
    window. See _relevant / _within_lookback for the exact filter.
    """
    config = flock_config()
    if config["disabled"]:
        raise RuntimeError("Flock is not configured")
    profile = config["profile_path"]
    chats = _fetch_inbox(profile, config["url"])

    # None from _fetch_inbox = fetch failed (subprocess crash, sidebar not
    # visible, non-JSON output, etc). Preserve the existing cache — a
    # transient failure shouldn't wipe the user's Today view for the next hour.
    # Raise a generic typed signal so structured health can mark the preserved
    # snapshot degraded without exposing worker output.
    if chats is None:
        existing = conn.execute("SELECT COUNT(*) AS n FROM flock_mentions_cache").fetchone()["n"]
        log.info("flock sync: fetch failed, keeping %d cached rows", existing)
        raise FlockRefreshError("Flock cache refresh failed; cached rows preserved")

    # Harvest a JID → display-name lookup from every 1:1 buddy row in the
    # sidebar. Used later to swap raw sender JIDs (u:2thc2ap...) for real
    # names in webhook-sourced mentions. Upsert so renames propagate on the
    # next sync; unknown-to-sidebar senders keep showing their JID.
    stamp = now_iso()
    contact_upserts = 0
    for c in chats:
        if c.get("type") == "buddy" and c.get("jid") and c.get("name"):
            conn.execute(
                "INSERT INTO flock_contacts (jid, name, updated_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(jid) DO UPDATE SET"
                " name = excluded.name, updated_at = excluded.updated_at",
                (c["jid"], c["name"], stamp),
            )
            contact_upserts += 1
    log.info("flock sync: harvested %d contact(s) from sidebar", contact_upserts)

    # Break down the drop reasons so a "nothing came out" outcome is diagnosable
    # from the log alone — otherwise successful-but-empty and failed-silently
    # look identical.
    n_scanned = len(chats)
    n_not_relevant = 0
    n_too_old = 0

    conn.execute("DELETE FROM flock_mentions_cache")
    inserted = 0
    for c in chats:
        if not _relevant(c):
            n_not_relevant += 1
            continue
        if not _within_lookback(c, config):
            n_too_old += 1
            continue
        conn.execute(
            "INSERT OR REPLACE INTO flock_mentions_cache ("
            " jid, name, is_group, has_mention, unread_count, last_message_time,"
            " is_muted, notify_on, bucket, mentions_json, synced_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                c.get("jid", ""),
                c.get("name", ""),
                1 if c.get("isGroup") else 0,
                1 if c.get("hasMention") else 0,
                int(c.get("unreadCount") or 0),
                _to_iso(c.get("lastMessageTime")),
                1 if c.get("isMuted") else 0,
                c.get("notifyOn") or "",
                c.get("bucket") or "",
                json.dumps(c.get("mentions") or []),
                stamp,
            ),
        )
        inserted += 1
    conn.commit()
    log.info(
        "flock sync: scanned=%d relevant=%d too_old=%d stored=%d",
        n_scanned, n_scanned - n_not_relevant, n_too_old, inserted,
    )
    return inserted


def todays_mentions(conn) -> list:
    """Read the cache, most-recent first. Used by /api/today and digest."""
    return conn.execute(
        "SELECT * FROM flock_mentions_cache"
        " ORDER BY has_mention DESC, last_message_time DESC"
    ).fetchall()


def deep_link_for(jid: str) -> str:
    """Public helper — the today router / digest may want the click-through URL."""
    return _flock_deep_link(jid)


def jid_to_name_map(conn) -> dict:
    """Return the full contacts JID → display-name mapping. Consumed by the
    today router to swap raw JIDs (u:2thc2ap...) for real names on webhook-
    sourced mentions where the sender only shows up as a JID."""
    return {
        r["jid"]: r["name"]
        for r in conn.execute("SELECT jid, name FROM flock_contacts")
    }


# ─── one-time auth flow (invoked by app.auth.flock) ─────────────────────

def run_auth_flow():
    """Open a headed Chromium against web.flock.com using the dedicated
    persistent profile. The user signs in, waits until the sidebar is visible,
    then presses ENTER in the terminal — this triggers a clean context.close(),
    which flushes cookies + IndexedDB to disk before we exit.

    We deliberately do NOT rely on the user closing the browser window: doing
    so risks Chromium being torn down mid-flush, leaving the profile without
    persisted session state. The terminal-ENTER gate keeps teardown ordered.
    """
    from playwright.sync_api import sync_playwright

    config = flock_config()
    profile = config["profile_path"]
    profile.mkdir(parents=True, exist_ok=True)
    _clean_stale_singleton_files(profile)
    print(f"Opening Chromium against {config['url']}")
    print(f"Profile directory: {profile}")
    print()
    print("Sign in to Flock in the window that opens.")
    print("Wait until your chat sidebar is fully loaded (you can see your channels).")
    print("Then come back here and press ENTER to finish — this saves the session.")
    print("(Do NOT close the browser window yourself.)")
    print()

    with sync_playwright() as pw:
        # Match the sync path exactly — same channel, same UA — so the login
        # session that gets persisted here works identically when replayed by
        # the headless sync tick.
        try:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                channel="chrome",
                headless=False,
                args=CHROMIUM_ARGS,
                user_agent=CHROME_UA,
                viewport={"width": 1440, "height": 900},
            )
        except Exception as exc:  # noqa: BLE001
            print(
                "note: chrome channel unavailable "
                f"({external_errors.exception_type(exc)}); using bundled Chromium."
            )
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=False,
                args=CHROMIUM_ARGS,
                user_agent=CHROME_UA,
                viewport={"width": 1440, "height": 900},
            )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(config["url"])
        try:
            input("[press ENTER once the Flock sidebar is loaded] ")
        finally:
            ctx.close()

    print("Auth complete — session persisted.")
    print("Trigger a sync: curl -X POST http://127.0.0.1:8000/api/sync")
