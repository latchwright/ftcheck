# SPDX-License-Identifier: MIT OR Apache-2.0
"""ftcheck must be installed to run the suite.

The compiled extension (`ftcheck._ftcheck`) lives beside the Python sources in
the installed package, so putting `python/` on `sys.path` would shadow the
package with a copy that has no extension in it. Build first:

    maturin develop        # or: maturin build && pip install --find-links dist ftcheck
"""
import pytest

try:
    import ftcheck  # noqa: F401
except ImportError as exc:  # pragma: no cover
    pytest.exit(f"ftcheck is not installed ({exc}). Run `maturin develop` first.", returncode=3)
