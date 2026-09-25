#!/usr/bin/env bash
# SPDX-License-Identifier: MIT OR Apache-2.0
# Asserts this repository cannot be published by accident.
#
# A guard that is merely present rots silently. This script is run by CI and by
# tests/test_publish_guards.py, one of which deliberately removes a guard and
# asserts that this script then fails.
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "PUBLISH GUARD FAILED: $1" >&2; exit 1; }

# 1. Every crate refuses `cargo publish`.
shopt -s nullglob
manifests=(crates/*/Cargo.toml)
[ ${#manifests[@]} -gt 0 ] || fail "no crate manifests found — guard cannot verify anything"
for m in "${manifests[@]}"; do
    grep -qE '^[[:space:]]*publish[[:space:]]*=[[:space:]]*false' "$m" \
        || fail "$m does not set publish = false"
done

# 2. PyPI rejects the upload.
grep -q 'Private :: Do Not Upload' pyproject.toml \
    || fail "pyproject.toml lost the 'Private :: Do Not Upload' classifier"

# 3. No release workflow exists.
release_workflows=(.github/workflows/*release* .github/workflows/*publish*)
[ ${#release_workflows[@]} -eq 0 ] || fail "a release/publish workflow exists: ${release_workflows[*]}"

# 4. No workflow invokes a publishing action.
if [ -d .github ] && grep -rqiE 'pypi-publish|cargo[[:space:]]+publish|twine[[:space:]]+upload|maturin[[:space:]]+publish' .github/; then
    fail "a workflow references a publishing command"
fi

echo "publish guards OK"
