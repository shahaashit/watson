"""Standalone Playwright driver invoked as a subprocess by flock_client.

Why a subprocess: `sync_playwright()` deadlocks when run from a uvicorn
threadpool worker (asyncio infrastructure around the thread interferes with
Playwright's greenlet-based sync API). A clean subprocess with its own event
loop avoids the whole problem, and gives us a natural hard-timeout knob
(subprocess.run's `timeout=` kills the process tree).

Contract:
  * exit 0, stdout = JSON string of the fiber-walk result → success
  * exit 0, stdout = "[]" (bracket-empty JSON) → login/layout/detection issue;
    the parent treats this identically to "nothing found"
  * exit non-zero, stderr → unhandled exception; parent surfaces it
  * Diagnostic files written to <data_dir>/flock-timeout.png etc. on failure

Invoked as: `python -m app.services.flock_worker <profile_dir> <url> [timeout_ms]`
"""
import json
import sys
import time
from pathlib import Path


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

# Polled by page.wait_for_function() after the sidebar container renders.
# Waits for the hydrated-row count to STABILIZE — otherwise Chrome hydrates
# rows in waves and walking too early misses the later ones. Counts the
# hydrated rows on each poll; when the count is > 0 AND unchanged for the
# past STABILITY_POLLS polls, we know Chrome has finished attaching props
# to the whole sidebar. Also iterates every row (not just the first) since
# the selector also matches section headers, which never carry `contact`.
HYDRATION_STABLE_JS = r"""
() => {
  const rows = document.querySelectorAll('#active-chats [role="button"][tabindex="0"]');
  if (rows.length === 0) return false;
  let hydrated = 0;
  for (const el of rows) {
    const key = Object.keys(el).find((k) =>
      k.indexOf('__reactFiber') === 0 || k.indexOf('__reactInternalInstance') === 0);
    if (!key) continue;
    let n = el[key];
    while (n) {
      if (n.memoizedProps && n.memoizedProps.contact && n.memoizedProps.contact.jid) {
        hydrated++;
        break;
      }
      n = n.return;
    }
  }
  // Cross-poll state — persists on window because wait_for_function runs
  // the same JS repeatedly against the same page context.
  const STABILITY_POLLS = 3;
  if (window.__watsonHydrated === undefined) {
    window.__watsonHydrated = -1;
    window.__watsonStable = 0;
  }
  if (hydrated > 0 && hydrated === window.__watsonHydrated) {
    window.__watsonStable++;
    if (window.__watsonStable >= STABILITY_POLLS) return true;
  } else {
    window.__watsonHydrated = hydrated;
    window.__watsonStable = 0;
  }
  return false;
}
"""


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

_SINGLETON_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def _log(msg):
    print(f"[flock_worker {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _clean_stale_singleton_files(profile_dir: Path):
    for name in _SINGLETON_FILES:
        try:
            (profile_dir / name).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            _log(f"could not remove stale {name} ({type(exc).__name__})")


def _dump_screenshot(page, dest_dir: Path, name: str):
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(dest_dir / name), full_page=True)
        _log(f"screenshot: {dest_dir/name}")
    except Exception as exc:
        _log(f"screenshot failed ({type(exc).__name__})")


def _run(profile_dir: Path, url: str, timeout_ms: int, screenshot_dir: Path):
    import os
    from playwright.sync_api import sync_playwright

    headless = os.environ.get("FLOCK_HEADLESS", "true").lower() != "false"
    _log(f"profile={profile_dir} url={url} headless={headless} timeout_ms={timeout_ms}")
    _clean_stale_singleton_files(profile_dir)

    with sync_playwright() as pw:
        # Prefer real Chrome so cookies encrypted by the auth flow decrypt.
        try:
            _log("launching Chrome (channel='chrome')")
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel="chrome",
                headless=headless,
                args=CHROMIUM_ARGS,
                user_agent=CHROME_UA,
                viewport={"width": 1440, "height": 900},
                timeout=15_000,
            )
            _log("launched real Chrome")
        except Exception as exc:
            _log(
                f"channel=chrome failed ({type(exc).__name__}); "
                "falling back to bundled Chromium"
            )
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=headless,
                args=CHROMIUM_ARGS,
                user_agent=CHROME_UA,
                viewport={"width": 1440, "height": 900},
                timeout=15_000,
            )

        try:
            ctx.set_default_timeout(timeout_ms)
            page = ctx.new_page()
            _log(f"navigating to {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            _log(f"page loaded at {page.url}")

            try:
                page.wait_for_selector("#active-chats", timeout=timeout_ms)
                _log("sidebar visible; waiting for hydrated-row count to stabilize")
                # Poll every ~250ms until the hydrated count is stable across
                # 3 consecutive polls. Robust to Chrome's wave-based rendering
                # without a brittle fixed sleep.
                page.wait_for_function(HYDRATION_STABLE_JS, timeout=timeout_ms, polling=250)
                _log("hydration stable")
            except Exception as exc:
                _log(f"sidebar not ready ({type(exc).__name__}); dumping state")
                _dump_screenshot(page, screenshot_dir, "flock-timeout.png")
                try:
                    (screenshot_dir / "flock-timeout.html").write_text(page.content()[:8000])
                except Exception as html_exc:
                    _log(f"html dump failed ({type(html_exc).__name__})")
                _log(f"final url: {page.url}")
                # Empty output = sidebar didn't render; parent treats as no-op.
                print("[]")
                return

            # No fixed settle needed — hydration wait above already proved
            # that at least one row has props. Walking is safe now.
            raw = page.evaluate(FIBER_WALK_JS)
            _log(f"fiber walk complete ({len(raw) if isinstance(raw, str) else 'non-str'} chars)")
            chats = json.loads(raw) if isinstance(raw, str) else raw

            # Sidebar-only: no click-into-chat step. Clicking a chat row
            # fires Flock's read-receipt path and hides the unread badge in
            # the user's real Flock client — silently swallowing messages
            # the user hadn't seen yet. The sidebar fiber already carries
            # hasMention / unreadCount / lastMessageTime, which is what the
            # Today card actually displays. Per-message previews stay in
            # Flock itself.

            sys.stdout.write(json.dumps(chats))
            sys.stdout.write("\n")
        finally:
            _log("closing context")
            ctx.close()
            _log("context closed")


def main():
    if len(sys.argv) < 3:
        print("usage: python -m app.services.flock_worker <profile_dir> <url> [timeout_ms] [screenshot_dir]",
              file=sys.stderr)
        sys.exit(2)
    profile_dir = Path(sys.argv[1]).expanduser()
    url = sys.argv[2]
    timeout_ms = int(sys.argv[3]) if len(sys.argv) > 3 else 20_000
    screenshot_dir = Path(sys.argv[4]).expanduser() if len(sys.argv) > 4 else Path("~/.watson").expanduser()
    _run(profile_dir, url, timeout_ms, screenshot_dir)


if __name__ == "__main__":
    main()
