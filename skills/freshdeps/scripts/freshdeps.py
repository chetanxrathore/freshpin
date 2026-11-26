#!/usr/bin/env python3
"""Run freshdeps without installing it.

Uses the copy of freshdeps bundled with the plugin (``<plugin root>/src``) when present,
otherwise an installed ``freshdeps`` package. Usage is identical to the ``freshdeps`` CLI:

    python3 scripts/freshdeps.py resolve pypi:fastapi eol:postgres --format agent
"""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info < (3, 10):  # noqa: UP036 - give a clear message on old interpreters
    sys.exit("freshdeps needs Python 3.10 or newer")

_HERE = Path(__file__).resolve()
# scripts/ -> freshdeps/ (skill) -> skills/ -> plugin root
_BUNDLED_SRC = _HERE.parents[3] / "src"
if (_BUNDLED_SRC / "freshdeps" / "__init__.py").is_file():
    sys.path.insert(0, str(_BUNDLED_SRC))

try:
    from freshdeps.cli import main
except ImportError:
    sys.exit(
        "freshdeps is not available: install the freshdeps Claude Code plugin, or install "
        "the CLI from its repository (see README). Do not `pip install freshdeps` - that "
        "PyPI name belongs to an unrelated project."
    )

if __name__ == "__main__":
    sys.exit(main())
