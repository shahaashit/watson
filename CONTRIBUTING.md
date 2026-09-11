# Contributing safely

This is a public, company-neutral repository. Treat every commit, pull request,
attachment, and CI log as public. These rules apply to people and coding agents.

## Never publish

- API keys, OAuth client secrets, access/refresh tokens, cookies, authorization
  headers, private keys, service-account credentials, or credential-store exports.
- Real setup bundles, environment files, private registry configuration, local
  databases/backups, logs, browser profiles, or exported integration responses.
- Internal hostnames, company email domains, employee identities, customer data,
  private repository paths, real task/MR IDs or links, or proprietary code.
- Screenshots, recordings, fixtures, documentation, commit messages, or PR text
  containing any of the above. Redacting a token alone does not anonymize a file.

Public provider names and public API URLs are fine. Company-specific values
must be supplied through local setup/settings, never hardcoded as defaults.
Use synthetic examples such as `alex@example.com`, `gitlab.example.com`, and
invented task titles. Do not copy real records and change just the person's name.
Do not add a denylist containing actual private company details to this repo.

## Keep configuration local

Keep credentials in the OS credential store and live data under
`WATSON_DATA_DIR`, outside the checkout. Shared `.watson-setup.json` files are
private credentials, not repository assets; distribute them only through an
approved private channel. Commit only sanitized example templates with empty
values or obvious placeholders. Never force-add an ignored private file.

## Before every commit and push

1. Check `git status --short` and review the full diff. Stage explicit paths;
   avoid blanket staging when local setup files or generated artifacts exist.
2. Run `./scripts/check-public-tree.sh` and `git diff --cached --check`.
3. Run `gitleaks git --pre-commit --staged --redact --no-banner` before committing.
   Before pushing, run `gitleaks git --redact --no-banner` to check commit history
   too: deleting a secret in a later commit does not remove it from history.
4. Review staged content, images, filenames, and commit/PR text for company or
   personal data. Secret scanners cannot establish that content is non-proprietary.
5. Stop if a check fails or a required checker is unavailable. Do not bypass a
   finding with an allowlist, scanner exclusion, or `--no-verify` just to publish.
   Ask the maintainer when ownership or sensitivity is unclear.

`.gitignore` does not protect already tracked files. The public-tree check and
secret scan are complementary, not substitutes for review. Local rules are not
server-side enforcement; mandatory GitHub checks require CI and branch rules.

## If something private is committed

Stop pushing. If a credential may have been exposed, revoke/rotate it promptly
and notify the maintainer privately without repeating the value. Removing the
file alone is insufficient. Coordinate any history cleanup and collaborator
re-cloning with the maintainer; never force-push or rewrite shared history
without explicit approval. Do not report a real secret in a public issue.
