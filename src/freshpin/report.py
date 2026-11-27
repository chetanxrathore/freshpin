"""Output formatting: human tables, JSON, and token-efficient ``agent`` text."""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, TextIO

from freshpin.auditors.base import FLAGGED
from freshpin.registries.base import Resolution
from freshpin.registries.endoflife import OFFICIAL_IMAGES

if TYPE_CHECKING:
    from freshpin.auditors import AuditReport

FORMATS = ("table", "json", "agent")


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
        hints.append(f"uses: {res.name}@{d.get('uses_ref') or res.version}")
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
        image = OFFICIAL_IMAGES.get(res.name)
        if image and cycle:
            hints.append(f"docker {image}:{d.get('docker_tag') or cycle}")
        if d.get("newest_cycle"):
            hints.append(f"newest non-LTS {d.get('newest')}")
    elif res.ecosystem == "npm" and d.get("deprecated"):
        hints.append(f"DEPRECATED: {d['deprecated']}")
    if d.get("prerelease_only"):
        hints.append("only pre-releases are published")
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


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def _summary_line(report: AuditReport) -> str:
    counts = report.counts()
    parts = [f"{n} {status}" for status, n in counts.items() if n]
    files = len(report.files)
    detail = ", ".join(parts) if parts else "nothing to check"
    return f"{len(report.findings)} checked in {files} file{'s' if files != 1 else ''}: {detail}"


def format_audit_agent(report: AuditReport) -> str:
    """Ultra-compact, instruction-style output for LLM agents."""
    flagged = report.flagged
    checked = len(report.findings)
    lines: list[str] = []
    if flagged:
        tally = Counter(f.status for f in flagged)
        if set(tally) == {"outdated"}:
            lines.append(
                f"freshpin: {len(flagged)} outdated of {checked} checked"
                " - upgrade to the latest stable versions:"
            )
        else:
            kinds = ", ".join(f"{tally[s]} {s}" for s in FLAGGED if tally[s])
            lines.append(
                f"freshpin: {len(flagged)} to fix of {checked} checked ({kinds})"
                " - use the versions shown:"
            )
        for f in flagged:
            # outdated lines carry the bump size; missing/yanked lines say what is wrong,
            # plus the bump size when the fix is an upgrade ([missing, major])
            tags: list[str] = [f.status] if f.status != "outdated" else []
            tags += [f.bump] if f.bump else []
            tail = f" [{', '.join(tags)}]" if tags else ""
            if f.note:
                tail += f" {f.note}"
            lines.append(
                f"{f.location(report.base)} | {f.current} -> {f.suggested or f.latest}{tail}"
            )
    else:
        lines.append(f"freshpin: OK - {len(report.findings)} checked, none outdated.")
    unverified = [
        f"{f.ref.ecosystem}:{f.ref.name} {f.note}" for f in report.findings if f.status == "error"
    ] + [
        f"{f.ref.ecosystem}:{f.ref.name} {f.ref.spec.strip()} may not exist"
        " (not in the cached version list, and it could not be checked live)"
        for f in report.unconfirmed
    ]
    if unverified:
        more = f" (+{len(unverified) - 5} more)" if len(unverified) > 5 else ""
        lines.append(f"unverified: {'; '.join(unverified[:5])}{more}")
    lines.extend(f"error: {e}" for e in report.errors)
    return "\n".join(lines)


_STATUS_COLOR = {
    "outdated": "red",
    "missing": "red",
    "yanked": "red",
    "error": "yellow",
    "ok": "green",
    "unpinned": "cyan",
}


def format_audit(
    report: AuditReport, fmt: str, *, color: bool = False, show_all: bool = False
) -> str:
    if fmt == "json":
        return json.dumps(report.to_dict(), indent=2)
    if fmt == "agent":
        return format_audit_agent(report)
    rows: list[list[str]] = []
    for f in report.findings:
        if f.status == "skipped" and not show_all:
            continue
        status = f.status + (f" ({f.bump})" if f.flagged and f.bump else "")
        status = paint(status, _STATUS_COLOR.get(f.status, "dim"), color)
        detail = f"-> {f.suggested}" if f.flagged and f.suggested else ""
        if f.note:
            detail = f"{detail}  {f.note}".strip()
        rows.append([f.location(report.base), f.current, f.latest or "?", status, detail])
    parts = []
    if rows:
        parts.append(
            render_table(
                ["LOCATION", "CURRENT", "LATEST", "STATUS", "ACTION / NOTE"], rows, color=color
            )
        )
    elif not report.findings:
        parts.append("No supported dependency declarations found.")
    parts.append(paint(_summary_line(report), "bold", color))
    parts.extend(paint(f"error: {e}", "yellow", color) for e in report.errors)
    return "\n".join(parts)
