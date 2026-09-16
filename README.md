# Watson — local Work OS for your Mac

Watson is a single-user localhost web app for deciding what deserves attention
and keeping its context together. It runs only on `127.0.0.1` and keeps its
live SQLite database on your Mac.

Contributing? Read [the public-repository safety rules](CONTRIBUTING.md) before
opening a PR or pushing changes. Credentials and company-specific data must
remain outside the public repository.

The app has three primary views:

- **My Work** — your active tasks with direct ClickUp links, all of today's
  meetings, and a compact capture/ask box. Priority ordering stays local.
  Review-tracking tasks are excluded from this personal view, without deleting them.
- **Team** — explicit tracked-person lanes plus permanent Others and
  Unassigned lanes. Each lane scrolls inside its card.
- **Log** — searchable work activity, captures, decisions, and approvals.

Settings is always available from the gear menu. The first launch opens the
same setup controls: profile, integrations, tracked people, connection checks,
and backup location. Everything remains editable later.

## Sync schedule and status

External synchronization runs every ten minutes, aligned to :00, :10, :20,
:30, :40, and :50 in the configured profile timezone (`cron minute="*/10"`).
Timezone changes preserve this schedule. Startup still warms caches in the
background, and **Sync now** uses the same pipeline. Overlapping full-sync
requests return `{"skipped":"already_running"}` without changing completion
metadata.

`GET /api/sync/status` is a local-only read suitable for a shared UI poll every
three seconds; polling does not contact external services. It preserves
`running`, `last_sync_at`, and `sources`, and adds:

- `last_sync_duration_seconds`: elapsed duration of the last completed attempt,
  or `null` before metrics exist.
- `last_sync_error_count`: numeric failure summary (default `0`), counting
  failed steps, failed/skipped exact ClickUp refreshes, and failed/uncertain
  review-automation outcomes. Benign busy skips and deferred work do not count.
- `sync_interval_seconds`: `600`.

Completion time, duration, and error count are persisted together in SQLite
`user_meta`, including partial failures; an unexpected pipeline abort records
one error. A running or skipped attempt leaves the previous completion visible
until the active attempt finishes. These metrics contain no raw error details.

While Watson is visible, one shared status poll checks for completed syncs every
three seconds. My Work (including its calendar), Team, Log,
and open work details then reload their cached data in place. Returning to a
hidden tab also refreshes these views. Drafts and filters stay intact; board
refreshes wait until dragging and priority saves finish. Settings uses the same
live connection status without resetting editable setup fields.

## Removing and restoring work

Use **Remove from Watson** in a work item's actions menu to hide it locally.
The confirmation does not delete or close anything in ClickUp or GitLab. Notes,
activity and source links are retained, and sync suppression prevents those
linked sources from recreating the removed work.

You can undo from the removed item's details or restore it under **Settings →
Data & Backup → Removed work**. Restoring preserves its lifecycle: a previously
completed item is not reopened just because it was restored.

Use **Add MR** in Linked work to attach another GitLab merge request to the
current work item. Paste its URL from your configured GitLab instance. An MR
already attached elsewhere is not silently moved or duplicated. ClickUp links
distinguish the original task from its review task, and repeated links to the
same task are shown once. Previously unlinked MRs can still be restored from
work details. Removal and restoration are recorded in the local activity log.
There is no permanent-delete operation in this workflow.

## Install on macOS

Requirements: macOS, Git, Python 3.10 or newer, and Node 18 or newer with npm.
The installer reuses a compatible Python (including Watson's existing virtual
environment). If none is found and Homebrew is available, it installs Python
3.12; otherwise it explains how to install Python before retrying. Existing
system Python and pip installations are not upgraded or modified. Missing pip
is bootstrapped only inside Watson's isolated virtual environment. Node/npm and
Git must already be installed. Windows and Linux
are not supported by the current installer and macOS Keychain integration.

While the repository is private, the owner must invite you as a collaborator;
accept that invitation and authenticate Git with your own GitHub account.
Clone into a permanent local folder: the service runs from this checkout.

```bash
git clone https://github.com/shahaashit/watson.git
cd watson
./scripts/install.sh
```

The installer creates the backend virtual environment, installs backend and
frontend dependencies, builds the frontend, installs the local LaunchAgent,
waits for a localhost health check, and opens onboarding. It does not require
an `.env` file or API tokens.

Normal installation does not download Chromium. Unchanged backend dependencies,
frontend dependencies, and frontend builds are reused on subsequent runs;
changed requirements or source files trigger the corresponding step. Each
install/build step reports its elapsed time. First installs still need to
download packages, so network speed and package mirrors affect duration.

Onboarding defaults to IST (`Asia/Kolkata`) with a timezone dropdown. Enter just
your email domain (for `alex@example.com`, use `example.com`). **Continue to
Integrations** validates and saves the profile before moving forward; there is
no separate save step. Existing saved timezones remain unchanged.

### Colleague setup: one shared file

Your team can supply a private **`.watson-setup.json`** containing its GitLab
application settings, ClickUp OAuth app credentials, and Google **Desktop** OAuth client configuration. Place
it in the repository root before installing (it is gitignored), or pass its path:

```bash
./scripts/install.sh --setup "$HOME/Downloads/.watson-setup.json"
```

The installer also accepts `WATSON_SETUP_FILE=/absolute/path/to/file.json`.
It parses JSON as data, never executes it as shell code, and never prints its
contents. The importer stores ClickUp and Google client credentials in macOS Keychain and
GitLab's non-secret application ID/server in the local database. It does not
sign anyone in automatically. Each colleague clicks **Connect GitLab** and
**Connect Google** or **Connect ClickUp**, authorizes their own account in the provider's browser
window, and selects their GitLab repositories or ClickUp Workspace, Space and destination List in Watson.

For an existing installation, import the file and restart:

```bash
(cd backend && .venv/bin/python -m app.auth.setup_bundle import /absolute/path/to/.watson-setup.json)
./scripts/restart.sh
```

Use [`watson-setup.example.json`](watson-setup.example.json) for the schema.
Any integration can be omitted. The shared file must contain only
application configuration: **never add personal GitLab tokens, Google access
or refresh tokens, AI keys, ClickUp tokens, or a GitLab client secret.**
Keep the real file private and out of source control, including renamed copies.
Google Desktop client configuration is distributed to installed clients; it
must not be replaced with a confidential Web/server OAuth client.

For ClickUp, a Workspace owner/admin registers an OAuth application with the
exact redirect URI `http://127.0.0.1:18766/callback`. Put its client ID and client
secret in the `clickup` section shown in the example file. This localhost-only
distribution means recipients can retrieve the shared app secret: distribute
only to trusted colleagues with the app owner's approval, never publicly.
Each person receives their own authorization token. After connecting, select
the Workspace, Space and List where Watson may create tasks, then save the
destination. Only authorized destinations are accepted. Connecting does not
create tasks; the separate review-automation preference governs future sync.
After reconnecting, select the destination again. Revoked access prompts
**Reconnect ClickUp**; it never silently falls back to an old personal token.
Onboarding and Settings use browser sign-in only. Existing token-based
connections are preserved until replaced by a successful OAuth connection.

Register a GitLab application as a **public client** (Confidential unchecked),
with scope `read_api` and this exact redirect URI:

```text
http://127.0.0.1:18765/callback
```

Use that application's ID in the shared file. Each GitLab instance needs its
own registration. Set `allow_http: true` only for an intentionally HTTP-only
instance: PKCE does not encrypt HTTP traffic. The read-only OAuth scope does
not permit GitLab mutations such as removing yourself as reviewer. A manually
configured token with appropriate permissions remains available under
**Manual connection settings** if those operations are needed.

Google's app owner must enable Calendar API and configure its OAuth audience,
test-user access, and any required verification. Sharing the client JSON does
not bypass Google's account or organization restrictions. Reimporting identical
configuration preserves existing user authorization; changing the Google client
requires a new sign-in. GitLab tokens refresh automatically; rejected grants
show **Reconnect GitLab** on the main interface while keeping cached work.

The app owner can export the configured application details into a new private
file (the command refuses to overwrite an existing file):

```bash
(cd backend && .venv/bin/python -m app.auth.setup_bundle export ../.watson-setup.json)
```

This file can also be supplied through deployment configuration in the future,
but this release still runs locally on macOS. Hosting a multi-user application
would require different redirect URIs, authentication, and credential storage.

If a network uses private package mirrors, create a local, uncommitted
`.watson-install.env` with only `PIP_INDEX_URL`, `PIP_TRUSTED_HOST`, and/or
`NPM_CONFIG_REGISTRY`. You can use [`.env.example`](.env.example) as a blank,
generic reference. This optional installer file is only for package mirrors,
not integration credentials. Settings no longer offers `.env` imports.

## First launch and integrations

The installer opens [onboarding](http://127.0.0.1:8000/onboarding). You can skip
integrations and configure them later in Settings. An AI provider is needed
for classification, Ask, and AI review grouping; manual work and notes do not
require one. No coding-agent application is required to run Watson.

1. **Profile:** save your name, timezone (for example `Europe/London`), and
   email domain. Replace `example.com` with the domain used by your team.
   Review task creation and AI grouping are independently enabled by default;
   disable either checkbox before connecting integrations if you do not want it.
2. **Integrations:** save your own connection details using the table below,
   then use **Test connection**. GitLab also needs a saved repository selection.
3. **People:** add each teammate's display name and identifier. The identifier
   is their GitLab username; Watson combines it with the profile email domain
   for email identities. This assumes their email local part matches that username.
4. **Ready:** choose your own backup folder, run **Sync now**, check the source
   health, and click **Start using Watson**. Save each form before continuing.

| Integration | What you supply | Steps outside Watson |
|---|---|---|
| AI provider | Select FastRouter and paste your own API key. Endpoint and model are prefilled under Advanced settings; existing custom configurations are preserved. | Open the FastRouter link, sign in, and create a project key for Watson with a spending limit. |
| GitLab | Click Connect GitLab after importing the setup file, then select repositories. | Sign in and approve read access on your GitLab instance. Your account must have access to the selected projects. |
| ClickUp | Connect ClickUp with shared app setup, then choose Workspace, Space and List. | Authorize your own account and Workspaces. Successful read access does not prove task-creation permissions. |
| Google Calendar (optional) | Click Connect Google after importing the setup file containing its Desktop client. | Authorize your own Google account in the browser opened by Connect Google. |

For token creation, see [GitLab personal tokens](https://docs.gitlab.com/user/profile/personal_access_tokens/)
and [token scopes](https://docs.gitlab.com/security/tokens/access_token_scopes/),
and [ClickUp personal-token authentication](https://developer.clickup.com/docs/authentication).
For a ClickUp list ID, open the intended List in ClickUp and inspect its URL;
use the list identifier, not a task, folder, workspace, or view identifier.
Private GitLab instances may require your organization's network or VPN.

### AI provider setup

Onboarding and Settings offer FastRouter with API-key authentication (not OAuth).
Use **Get a FastRouter API key**, open your project's Keys section, and create a
key for Watson. Paste it into Watson, save, and use **Test connection**. The key
is stored in macOS Keychain; leave the replacement field blank to keep it, or
paste a new key and save after revoking/rotating an old one. Endpoint changes
in the form require a replacement key so an existing key is not reused for
another host accidentally.

**Advanced settings** retains the Anthropic-compatible endpoint and model.
Existing custom connections are not overwritten. Each colleague should use
their own key; do not add personal AI keys or administrator provisioning keys
to the shared setup file.

### Google Calendar setup

Follow Google's [Desktop OAuth setup guide](https://developers.google.com/workspace/calendar/api/quickstart/python):
enable the Google Calendar API in your Google Cloud project, configure the
consent screen (and test users if applicable), create an OAuth client of type
**Desktop app**, and download its JSON. Paste the JSON into Watson's Google
Calendar field and save it. Click **Connect Google** and complete authorization
in the browser, then test the connection. You do not need to run Google's
sample application or copy its code into Watson.

Watson requests `calendar.events`, which allows reading and writing calendar
events. Account or organization policy may require administrator approval.
Watson stores the client configuration and resulting authorization in Keychain;
no OAuth JSON file needs to be placed in the repository.

## Sharing and required files

Share the GitHub repository link and grant access while it is private. A fresh
clone contains all application code, prompts, dependency manifests, and install
scripts. For the guided team setup, also share **only `.watson-setup.json`**
as described above. Without that file, recipients can use local work and AI
features; OAuth integrations require a setup file from their administrator.
Each person starts with an empty local database
and authorizes their own accounts; no developer database or personal tokens
are needed.

| Files or data | Where they come from |
|---|---|
| `backend/`, `frontend/`, `scripts/`, README, `.env.example` | Included in the clone. Keep the checkout in place while the service runs. |
| `backend/.venv/`, `frontend/node_modules/`, `frontend/dist/` | Generated by the installer. |
| `~/.watson/watson.db`, `~/.watson/server.log` | Created locally by Watson. |
| `~/Library/LaunchAgents/com.watson.local.plist` | Generated by the installer for that user's checkout. |
| API keys and Google OAuth credentials | Supplied by the recipient and stored in macOS Keychain. |
| `.watson-install.env` | Optional, created locally only for a private package mirror. |
| `.watson-setup.json` | Optional private application configuration supplied by the team. Contains no personal access or refresh tokens. |

Do not send your `.env`, `.npmrc`, `.watson-install.env`, live database, backups,
logs, OAuth files, Keychain credentials, or browser profiles. Share through GitHub
instead of zipping your working directory, which can contain ignored private files.
The `.env.example` template is optional; normal onboarding does not require
creating or importing an `.env` file.

## Private configuration and approval boundary

Set up Anthropic-compatible capture/Ask, GitLab, ClickUp, Google Calendar from Settings. Token and OAuth fields are write-only and stored
in macOS Keychain. The UI shows only whether a credential is present.

Watson can be useful with no integrations: add and prioritize local work,
keep notes, and use the Log. A pasted GitLab MR or
ClickUp task link can be imported manually. GitLab collection may also discover
ClickUp work when a branch has `_clickup<id>` (for example
`feature/cleanup_clickup86abc1234`); Watson fetches that exact task instead of
performing broad recurring ClickUp scans.

ClickUp comments, task updates, closures, and ordinary task creation are always
drafts that require **Approve**. Automatic creation of `Review - …` tasks is a
separate setting, enabled by default with an option to disable it, and is idempotent; it can be
disabled from the profile. Dragging or reordering never changes an external
assignee, priority, or status.

The profile also stores the email domain used to derive team identities for
ClickUp and Calendar. It defaults to `example.com` for a new install and
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

## Troubleshooting a new installation

- **Clone says repository not found:** accept the collaborator invitation and
  check that Git is authenticated as the invited account.
- **Missing Python or Node:** check `python3 --version`, `node --version`, and
  `npm --version` in the terminal running the installer, then install the missing prerequisite.
- **Dependency download fails:** confirm access to the package registries and
  Playwright browser downloads. If your network needs a mirror, use the optional
  `.watson-install.env` settings described above and rerun `./scripts/install.sh`.
- **Port 8000 is occupied:** inspect with `lsof -nP -iTCP:8000 -sTCP:LISTEN`.
  Stop or reconfigure the application you recognize as using it, then rerun the
  installer. Watson's installer refuses to stop an unrelated service.
- **Browser does not open or service does not start:** open
  `http://127.0.0.1:8000/onboarding` manually. Inspect
  `launchctl print gui/$(id -u)/com.watson.local` and `~/.watson/server.log`.
  If you moved the checkout, rerun the installer from its new location.
- **Keychain unavailable:** unlock your macOS login Keychain, address any
  access prompt, and retry saving the integration. If a token is expired,
  replace it in Settings and test the connection.
- **No MRs or review tasks:** confirm GitLab repository selection and access,
  ClickUp destination list and permissions, and the review-creation checkbox;
  run Sync now and inspect source health and Log.
- **Google authorization fails:** verify the Desktop OAuth client, enabled
  Calendar API, and account/test-user permissions, then use Connect/Reconnect Google.
- **GitLab sign-in does not start:** confirm the shared configuration is imported
  and port 18765 is free. A cancelled browser flow times out after five minutes.
  The GitLab app must be public, with `read_api` and the exact callback above.

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
