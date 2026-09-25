#!/usr/bin/env bash
# SPDX-License-Identifier: MIT OR Apache-2.0
#
# Asserts every source file carries the SPDX licence header.
#
# Fixtures and test data are exempt on purpose: tests assert line numbers inside
# them (ThreadSanitizer reports cite `src/lib.rs:14:9`), so a header would shift
# every expected location.
set -euo pipefail
cd "$(dirname "$0")/.."

header='SPDX-License-Identifier: MIT OR Apache-2.0'
missing=()
while IFS= read -r -d '' f; do
    head -n 3 "$f" | grep -qF "$header" || missing+=("$f")
done < <(find crates python scripts tests -type f \( -name '*.rs' -o -name '*.py' -o -name '*.sh' \) \
    -not -path 'tests/data/*' -print0)

if [ ${#missing[@]} -gt 0 ]; then
    echo "missing licence header ('$header') in:" >&2
    printf '  %s\n' "${missing[@]}" >&2
    exit 1
fi
echo "licence headers: ok"
