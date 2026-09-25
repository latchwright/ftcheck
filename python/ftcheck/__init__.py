# SPDX-License-Identifier: MIT OR Apache-2.0
"""ftcheck — free-threaded correctness harness for Rust/PyO3 extensions."""
from __future__ import annotations

import json
from typing import Any

__version__ = "0.0.0"

__all__ = ["__version__", "lint"]


def lint(
    path: str,
    min_confidence: str = "certain",
    pyo3_version: str | None = None,
) -> dict[str, Any]:
    """Lint a crate, returning findings alongside what was examined."""
    from ftcheck import _ftcheck

    return json.loads(
        _ftcheck.lint_crate_json(str(path), pyo3_version, min_confidence)
    )
