# SPDX-License-Identifier: MIT OR Apache-2.0
"""`python -m ftcheck`, for environments where the console script is not on PATH."""
from ftcheck.cli import main

raise SystemExit(main())
