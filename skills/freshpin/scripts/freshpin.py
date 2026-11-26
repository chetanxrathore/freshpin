#!/usr/bin/env python3
"""Run freshpin without installing it.

Uses the copy of freshpin bundled with the plugin (``<plugin root>/src``) when present,
otherwise an installed ``freshpin`` package. Usage is identical to the ``freshpin`` CLI:

    python3 scripts/freshpin.py resolve pypi:fastapi eol:postgres --format agent
"""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info < (3, 9):  # noqa: UP036 - give a clear message on old interpreters
    sys.exit("freshpin needs Python 3.9 or newer")

_HERE = Path(__file__).resolve()
# scripts/ -> freshpin/ (skill) -> skills/ -> plugin root
_BUNDLED_SRC = _HERE.parents[3] / "src"
if (_BUNDLED_SRC / "freshpin" / "__init__.py").is_file():
    sys.path.insert(0, str(_BUNDLED_SRC))

try:
    from freshpin.cli import main
except ImportError:
    sys.exit(
        "freshpin is not available: install the freshpin Claude Code plugin, or install "
        "the CLI with `uv tool install freshpin` / `pipx install freshpin` (see README)."
    )

if __name__ == "__main__":
    sys.exit(main())
