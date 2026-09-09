#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$repo_root"

failed=0
private_paths="$(git ls-files | rg '^(\.superpowers/|docs/superpowers/)' || true)"
if [[ -n "$private_paths" ]]; then
  echo "Private planning artifacts are tracked:"
  echo "$private_paths"
  failed=1
fi

tracked_files=()
while IFS= read -r file; do
  tracked_files+=("$file")
done < <(git ls-files | rg -v '^scripts/check-public-tree[.]sh$')

if ((${#tracked_files[@]})) && rg -n --no-heading '(/Users/[^/[:space:]]+|/home/[^/[:space:]]+|Library/CloudStorage)' "${tracked_files[@]}"; then
  failed=1
fi

sensitive_paths="$(git ls-files | rg -i '(^|/)([.]env$|[.]npmrc$|.*[.](pem|key|p12|pfx|db|sqlite|log)$|id_rsa|credentials[.]json|token[.]json)' || true)"
if [[ -n "$sensitive_paths" ]]; then
  echo "Credential-bearing or local-data filenames are tracked:"
  echo "$sensitive_paths"
  failed=1
fi

if [[ -f frontend/package-lock.json ]] && rg -n --pcre2 '"resolved": "https://(?!registry[.]npmjs[.]org/)' frontend/package-lock.json; then
  echo "The npm lockfile contains non-public resolved package URLs."
  failed=1
fi

if ((failed)); then
  echo "Public tree check failed."
  exit 1
fi

echo "Public tree check passed."
