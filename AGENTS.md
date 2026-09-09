# Watson contributor guide

Watson is a localhost, single-user work assistant built with FastAPI, SQLite,
React, and Vite. It combines locally captured notes with cached ClickUp,
GitLab, Google Calendar, and optional Flock context.

## Safety invariants

- Persist captured text before invoking an LLM so classification failures never
  lose input.
- Store live data outside the repository, under `WATSON_DATA_DIR`.
- Keep credentials in the operating-system credential store. Never serialize
  secrets into API responses, SQLite settings, logs, fixtures, or commits.
- Most external mutations require an explicit pending-action approval. The one
  opt-in exception is automatic creation of ClickUp tasks for review work; it
  must remain independently configurable and idempotent.
- Bind the application to `127.0.0.1`; Watson has no multi-user authentication.

## Product conventions

- Primary views are My Work, Team, and Log. Avoid adding navigation unless the
  information cannot fit one of those views.
- Watson-created work uses only `Discussion - …` and `Review - …` prefixes.
- GitLab and ClickUp page reads use local caches; synchronization performs the
  network work.
- The profile email domain is configurable and is used to derive identities for
  integrations that identify people by email.

## Development

- Backend tests: `backend/.venv/bin/python -m pytest backend/tests -q`
- Frontend tests: `cd frontend && npm test`
- Production build: `cd frontend && npm run build`
- Public-tree policy: `./scripts/check-public-tree.sh`
- Browser smoke test: `backend/.venv/bin/python scripts/verify-ui.py`

Tests must use a scratch `WATSON_DATA_DIR`; never mutate a developer's live
database. After code changes, `./scripts/restart.sh` rebuilds the frontend and
restarts the local launch agent.
