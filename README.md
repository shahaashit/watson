# Watson — local Work OS for your Mac

Watson is a single-user localhost web app for deciding what deserves attention
and keeping its context together. It runs only on `127.0.0.1` and keeps its
live SQLite database on your Mac.

The app has three primary views:

- **My Work** — your four local states: Today, Next, Waiting, and Done.
- **Team** — explicit tracked-person lanes plus permanent Others and
  Unassigned lanes. Each lane scrolls inside its card.
- **Log** — searchable work activity, captures, decisions, and approvals.

Settings is always available from the gear menu. The first launch opens the
same setup controls: profile, integrations, tracked people, connection checks,
and backup location. Everything remains editable later.

## Install on macOS

Requirements: macOS, Python 3.10 or newer, and Node 18 or newer.

```bash
git clone <your-watson-repository-url>
cd Watson
./scripts/install.sh
```

The installer creates the backend virtual environment, installs backend and
frontend dependencies, builds the frontend, installs the local LaunchAgent,
waits for a localhost health check, and opens onboarding. It does not require
an `.env` file or API tokens.

If a network uses private package mirrors, create a local, uncommitted
`.watson-install.env` with only `PIP_INDEX_URL`, `PIP_TRUSTED_HOST`, and/or
`NPM_CONFIG_REGISTRY`. You can use [`.env.example`](.env.example) as a blank,
generic reference. Existing installations may instead use **Import .env** in
Settings; that is an explicit migration action, not the default setup path.

## Private configuration and approval boundary

Set up Anthropic-compatible capture/Ask, GitLab, ClickUp, Google Calendar, and
optional Flock from Settings. Token and OAuth fields are write-only and stored
in macOS Keychain. The UI shows only whether a credential is present.

Watson can be useful with no integrations: add local work manually, move it
between the four states, keep notes, and use the Log. A pasted GitLab MR or
ClickUp task link can be imported manually. GitLab collection may also discover
ClickUp work when a branch has `_clickup<id>` (for example
`feature/cleanup_clickup86abc1234`); Watson fetches that exact task instead of
performing broad recurring ClickUp scans.

ClickUp comments, task updates, closures, and ordinary task creation are always
drafts that require **Approve**. Automatic creation of `Review - …` tasks is a
separate, opt-in setting (enabled by default) and is idempotent; it can be
disabled from the profile. Dragging or reordering never changes an external
assignee, priority, or status.

The profile also stores the email domain used to derive team identities for
ClickUp, Calendar, and Flock. It defaults to `example.com` for a new install and
can be changed during onboarding or later in Settings.

## Data and backups

The live database defaults to `~/.watson/watson.db` and must stay on local
storage. Do not place it in a cloud-synced directory: SQLite can be corrupted
if a sync client copies it mid-write.

Settings can point the backup destination at an already-synced folder. Watson
writes a daily journal and a safe SQLite backup there; that destination is for
copies, not the live database. A pre-Work-OS backup is made during the
additive migration, and old captures, reminders, actions, and cached context
are preserved.

## Run, restart, and uninstall

The installer creates `~/Library/LaunchAgents/com.watson.local.plist`. It runs
Watson from this checkout on port 8000, at [http://127.0.0.1:8000](http://127.0.0.1:8000),
and writes logs to `~/.watson/server.log`.

```bash
# Rebuild the frontend and restart the local service after a code change.
./scripts/restart.sh

# Stop the service for this login session.
launchctl bootout gui/$(id -u)/com.watson.local

# Remove the local service. Your local Watson data remains untouched.
launchctl bootout gui/$(id -u)/com.watson.local || true
rm -f ~/Library/LaunchAgents/com.watson.local.plist
```

For active frontend development, `./scripts/dev.sh` runs Vite on port 5173.
Do not run it alongside the LaunchAgent on port 8000.

## Verify a checkout

All ordinary tests use temporary SQLite data and mocked external services.

```bash
cd backend && .venv/bin/python -m pytest tests/ -v
cd ../frontend && npm test && npm run build
cd ..
bash -n scripts/install.sh scripts/install-launch-agent.sh scripts/verify-migration.sh
./scripts/verify-migration.sh
backend/.venv/bin/python scripts/verify-ui.py
```

`verify-migration.sh` creates and removes a unique temporary legacy database;
it never touches `~/.watson`. `verify-ui.py` starts its own scratch server on
port 8011, verifies desktop and narrow layouts, then terminates only that
child process and removes its temporary data.
