# SPDX-License-Identifier: MIT OR Apache-2.0
"""The exit-code contract.

`UNAVAILABLE` is the one that earns its place. ftcheck never claims code is
safe, only that no race was detected *on the exercised surface* — so a run that
never happened must not be reportable as a clean one. A sanitizer build that
failed to start has exercised nothing, and returning 0 there would turn an
infrastructure failure into a green check.
"""

CLEAN = 0
"""No confirmed finding on the exercised surface."""

FINDINGS = 1
"""At least one confirmed finding. Fail CI."""

USAGE = 2
"""Usage or configuration error."""

UNAVAILABLE = 3
"""Could not run: toolchain, image or interpreter failure."""

SUITE_FAILED = 4
"""`ci` only: the project's tests failed under the instrumented, threaded run,
and ThreadSanitizer reported nothing in the extension. A real failure, but not
a finding ftcheck made — often a test that is not thread-safe (mark it
`@pytest.mark.thread_unsafe`). Kept apart from FINDINGS so a CI pipeline can
tell "you have a race" from "your suite does not run in threads"."""
