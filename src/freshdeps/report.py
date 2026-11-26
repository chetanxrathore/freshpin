"""Output formatting: human tables, JSON, and token-efficient ``agent`` text."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Sequence
from typing import Any, TextIO

from freshdeps.registries.base import Resolution
from freshdeps.registries.endoflife import DOCKER_IMAGES

FORMATS = ("table", "json", "agent")

# endoflife.date product -> official Docker Hub image name (for hints).
_PRODUCT_TO_IMAGE: dict[str, str] = {}
for _image, _product in DOCKER_IMAGES.items():
    if "/" not in _image:
        _PRODUCT_TO_IMAGE.setdefault(_product, _image)


def use_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_COLORS = {"red": "31", "green": "32", "yellow": "33", "dim": "2", "bold": "1", "cyan": "36"}


def paint(text: str, color: str, enabled: bool) -> str:
    return f"\033[{_COLORS[color]}m{text}\033[0m" if enabled else text


def render_table(headers: Sequence[str], rows: Sequence[Sequence[str]], *, color: bool) -> str:
    """Render a plain aligned table (ANSI colours are ignored when measuring widths)."""
    ansi = _ANSI_RE
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(ansi.sub("", cell)))

    def line(cells: Sequence[str]) -> str:
        parts = []
        for i, cell in enumerate(cells):
            pad = widths[i] - len(ansi.sub("", cell))
            parts.append(cell + " " * pad if i < len(cells) - 1 else cell)
        return "  ".join(parts).rstrip()

    out = [paint(line(headers), "bold", color)]
    out.extend(line(r) for r in rows)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def resolution_hints(res: Resolution) -> list[str]:
    """Short, actionable usage hints for a resolved version."""
    hints: list[str] = []
    d = res.details
    if res.ecosystem == "gha" and res.version:
        hints.append(f"uses: {res.name}@{d.get('major_tag') or res.version}")
    elif res.ecosystem == "eol" and res.version:
        cycle = d.get("cycle")
        lts = " LTS" if d.get("lts") else ""
        if cycle:
            hints.append(f"cycle {cycle}{lts}")
        eol = d.get("eol")
        if isinstance(eol, str):
            hints.append(f"EOL {eol}")
        elif eol is False:
            hints.append("no EOL date yet")
        image = _PRODUCT_TO_IMAGE.get(res.name)
        if image and cycle:
            hints.append(f"docker {image}:{cycle}")
        if d.get("newest_cycle"):
            hints.append(f"newest non-LTS {d.get('newest')}")
    elif res.ecosystem == "npm" and d.get("deprecated"):
        hints.append(f"DEPRECATED: {d['deprecated']}")
    if res.stale:
        hints.append("stale cache (offline)")
    return hints


def format_resolutions(resolutions: Sequence[Resolution], fmt: str, *, color: bool = False) -> str:
    if fmt == "json":
        return json.dumps([r.to_dict() for r in resolutions], indent=2)
    if fmt == "agent":
        lines = []
        for r in resolutions:
            if not r.ok:
                lines.append(f"{r.query} ERROR {r.error}")
                continue
            hints = resolution_hints(r)
            suffix = f" ({'; '.join(hints)})" if hints else ""
            lines.append(f"{r.query} {r.version}{suffix}")
        return "\n".join(lines)
    rows: list[list[str]] = []
    for r in resolutions:
        if r.ok:
            version = paint(r.version or "", "green", color)
            notes = " · ".join(resolution_hints(r))
        else:
            version = paint("error", "red", color)
            notes = r.error or ""
        rows.append([r.query, version, notes])
    return render_table(["QUERY", "LATEST", "NOTES"], rows, color=color)


def dumps(data: Any) -> str:
    return json.dumps(data, indent=2)


def write(text: str, stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    out.write(text)
    if text and not text.endswith("\n"):
        out.write("\n")
