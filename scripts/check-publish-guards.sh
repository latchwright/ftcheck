#!/usr/bin/env bash
# SPDX-License-Identifier: MIT OR Apache-2.0
# Asserts ftcheck can be published in exactly one way.
#
# Only the Python package ships, only from .github/workflows/release.yml, only
# when a `v*` tag is pushed, and only to PyPI through Trusted Publishing (OIDC),
# so no upload token exists to leak. The crates are internal and never go to
# crates.io.
#
# A guard that is merely present rots silently. This script is run by CI and by
# tests/test_publish_guards.py, which breaks each guard in a copy of the tree and
# asserts that this script then fails.
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "PUBLISH GUARD FAILED: $1" >&2; exit 1; }

release=.github/workflows/release.yml

# 1. Every crate refuses `cargo publish`.
shopt -s nullglob
manifests=(crates/*/Cargo.toml)
[ ${#manifests[@]} -gt 0 ] || fail "no crate manifests found — guard cannot verify anything"
for m in "${manifests[@]}"; do
    grep -qE '^[[:space:]]*publish[[:space:]]*=[[:space:]]*false' "$m" \
        || fail "$m does not set publish = false"
done

# 2. The release workflow is the only file that publishes anything.
[ -f "$release" ] || fail "$release is missing"
publishing='pypi-publish|cargo[[:space:]]+publish|twine[[:space:]]+upload|maturin[[:space:]]+(publish|upload)|uv[[:space:]]+publish|upload\.pypi\.org|test\.pypi\.org'
while IFS= read -r f; do
    [ "$f" = "$release" ] || fail "$f publishes; only $release may"
done < <(grep -rlEi "$publishing" .github/ action.yml 2>/dev/null || true)

# Comments are not configuration: strip them before inspecting the workflow.
body=$(sed -E 's/(^|[[:space:]])#.*$//' "$release")

# 3. It publishes through the PyPA action and nothing else.
grep -qE 'uses:[[:space:]]*pypa/gh-action-pypi-publish@' <<<"$body" \
    || fail "$release does not publish with pypa/gh-action-pypi-publish"
if grep -qEi 'cargo[[:space:]]+publish|twine[[:space:]]+upload|maturin[[:space:]]+(publish|upload)|uv[[:space:]]+publish|upload\.pypi\.org|test\.pypi\.org' <<<"$body"; then
    fail "$release publishes by a route other than pypa/gh-action-pypi-publish"
fi

# 4. With Trusted Publishing: an OIDC token, and no stored credential anywhere.
grep -qE '^[[:space:]]+id-token:[[:space:]]*write[[:space:]]*$' <<<"$body" \
    || fail "$release does not grant id-token: write, so Trusted Publishing cannot work"
grep -qE '^[[:space:]]+environment:[[:space:]]*pypi[[:space:]]*$' <<<"$body" \
    || fail "$release does not publish from the 'pypi' environment"
if grep -qE 'secrets\.' <<<"$body"; then
    fail "$release references a secret; Trusted Publishing needs none"
fi
if grep -qEi '^[[:space:]]+(password|user|username|repository-url|repository_url):' <<<"$body"; then
    fail "$release passes a credential or repository to the publish action"
fi

# 5. It runs on a pushed `v*` tag and on nothing else.
trigger=$(awk '
    /^on:/                   { on = 1; next }
    on && /^[^[:space:]]/    { exit }
    on && NF                 { gsub(/[[:space:]]+$/, ""); print }
' <<<"$body")
expected=$'  push:\n    tags: [\'v*\']'
[ "$trigger" = "$expected" ] \
    || fail "$release must be triggered only by 'push: tags: [v*]', found:"$'\n'"$trigger"

echo "publish guards OK"
