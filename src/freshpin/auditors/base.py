"""Shared audit model: dependency references, findings and version evaluation."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Literal

from freshpin.registries.base import Resolution
from freshpin.registries.endoflife import (
    Cycle,
    cycles_from_details,
    match_codename,
    match_cycle,
    sort_cycles,
)
from freshpin.versioning import (
    Bump,
    Constraint,
    ConstraintError,
    Version,
    assess,
    bump_kind,
    meets_level,
    parse_pep440,
    parse_semver_range,
    parse_version,
    rewrite_spec,
    truncate,
)

Status = Literal["outdated", "ok", "unpinned", "skipped", "error"]
STATUSES: tuple[Status, ...] = ("outdated", "error", "ok", "unpinned", "skipped")

# How a spec should be interpreted.
#   pep508  - PEP 440 specifier (pyproject / requirements)
#   poetry  - Poetry/npm style range, bare version = exact
#   npm     - npm range, bare version = exact
#   cargo   - Cargo range, bare version = caret
#   gha     - GitHub Action ref (v4, v4.1.2, or a SHA with a version comment)
#   cycle   - a pinned runtime/image cycle (python:3.12, python-version: "3.12")
#   runtime - a supported-runtime *floor* (requires-python, engines.node)
Kind = Literal["pep508", "poetry", "npm", "cargo", "gha", "cycle", "runtime"]

IGNORE_MARKER_RE = re.compile(r"freshpin\s*:\s*ignore", re.IGNORECASE)
# Upgrading these across a major version changes the on-disk data format.
DATABASE_PRODUCTS = {
    "postgresql",
    "mysql",
    "mariadb",
    "mongodb",
    "redis",
    "valkey",
    "elasticsearch",
    "apache-cassandra",
    "apache-couchdb",
    "neo4j",
    "influxdb",
    "clickhouse",
}


@dataclass(frozen=True)
class DependencyRef:
    """One version declaration found in a file."""

    file: Path
    line: int
    ecosystem: str
    name: str
    spec: str
    kind: Kind
    display_name: str
    template: str  # how the declaration reads, with "{spec}" where the version goes
    section: str = ""
    syntax: str = ""  # for kind == "runtime": "pep440" or "semver"
    eol_only: bool = False  # for kind == "cycle": only flag end-of-life entries
    sha: str = ""  # for kind == "gha": the pinned commit SHA
    image: str = ""  # for Docker images: the repository as written (e.g. "postgres")
    static_status: Status | None = None  # decided without a lookup (skipped/unpinned)
    note: str = ""

    def render(self, spec: str) -> str:
        return self.template.replace("{spec}", spec)

    @property
    def current(self) -> str:
        return self.render(self.spec)

    @property
    def needs_lookup(self) -> bool:
        return self.static_status is None


@dataclass(frozen=True)
class Finding:
    ref: DependencyRef
    status: Status
    latest: str | None = None
    suggested: str | None = None
    bump: Bump | None = None
    note: str = ""
    verified: bool | None = None  # Docker: suggested tag confirmed on Docker Hub

    @property
    def current(self) -> str:
        return self.ref.current

    def location(self, base: Path | None = None) -> str:
        return f"{display_path(self.ref.file, base)}:{self.ref.line}"

    def to_dict(self, base: Path | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "file": display_path(self.ref.file, base),
            "line": self.ref.line,
            "ecosystem": self.ref.ecosystem,
            "name": self.ref.display_name,
            "lookup": f"{self.ref.ecosystem}:{self.ref.name}",
            "section": self.ref.section or None,
            "current": self.current,
            "spec": self.ref.spec,
            "status": self.status,
            "latest": self.latest,
            "suggested": self.suggested,
            "bump": self.bump,
        }
        if self.note:
            out["note"] = self.note
        if self.verified is not None:
            out["verified"] = self.verified
        return out


def display_path(path: Path, base: Path | None = None) -> str:
    base = base or Path.cwd()
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------------------
# small text helpers shared by the line-oriented parsers
# ---------------------------------------------------------------------------

_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|\'([^\']*)\'')
_INLINE_TABLE_RE = re.compile(r"\{[^{}]*\}")


def strip_comment(line: str) -> str:
    """Drop a trailing ``# comment`` that is not inside a quoted string."""
    quote: str | None = None
    escaped = False
    for i, ch in enumerate(line):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#":
            return line[:i]
    return line


def quoted_strings(text: str, *, skip_inline_tables: bool = True) -> list[str]:
    """All TOML string literals in ``text`` (optionally ignoring ``{ ... }`` tables)."""
    if skip_inline_tables:
        text = _INLINE_TABLE_RE.sub("", text)
    return [m.group(1) if m.group(1) is not None else m.group(2) for m in _STRING_RE.finditer(text)]


def bracket_delta(text: str) -> int:
    """``[`` minus ``]`` outside of string literals."""
    depth = 0
    for chunk in _STRING_RE.split(text)[::3]:
        if chunk:
            depth += chunk.count("[") - chunk.count("]")
    return depth


def unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def is_ignored(raw_line: str) -> bool:
    return bool(IGNORE_MARKER_RE.search(raw_line))


def with_status(ref: DependencyRef, status: Status, note: str) -> DependencyRef:
    """Mark ``ref`` as decided without a lookup (e.g. skipped git dependency)."""
    return replace(ref, static_status=status, note=note)


def honour_ignore(ref: DependencyRef, raw_line: str) -> DependencyRef:
    """Apply an inline ``# freshpin: ignore`` marker."""
    if is_ignored(raw_line):
        return with_status(ref, "skipped", "ignored (freshpin: ignore)")
    return ref


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------

_FALLBACK_OP = {"pep508": ">=", "poetry": "^", "npm": "^", "cargo": ""}


def _parse_constraint(kind: str, spec: str) -> Constraint:
    if kind == "pep508" or (kind == "runtime" and spec and spec.lstrip()[:1] in "<>=!~"):
        try:
            return parse_pep440(spec)
        except ConstraintError:
            if kind == "pep508":
                raise
    return parse_semver_range(spec, bare="caret" if kind == "cargo" else "exact")


def evaluate(
    ref: DependencyRef,
    resolution: Resolution | None,
    *,
    level: Bump = "minor",
    today: date | None = None,
    floors: bool = True,
) -> Finding:
    """Compare one reference against its resolved latest version.

    ``floors=False`` (library mode) only reports constraints that exclude the latest
    release, not ranges whose lower bound is merely old.
    """
    if ref.static_status is not None:
        return Finding(ref, ref.static_status, note=ref.note)
    if resolution is None or not resolution.ok or resolution.version is None:
        reason = resolution.error if resolution is not None else "not resolved"
        return Finding(ref, "error", note=f"lookup failed: {reason}")
    latest = parse_version(resolution.version)
    if latest is None:
        return Finding(ref, "error", note=f"unparseable latest version {resolution.version!r}")
    today = today or date.today()
    if ref.kind == "gha":
        finding = _evaluate_gha(ref, resolution, latest, level)
    elif ref.kind == "cycle":
        finding = _evaluate_cycle(ref, resolution, latest, level, today)
    elif ref.kind == "runtime":
        finding = _evaluate_runtime(ref, resolution, latest, today)
    else:
        finding = _evaluate_constraint(ref, resolution.version, latest, level, floors)
    extra = [n for n in (ref.note, finding.note) if n]
    deprecated = resolution.details.get("deprecated")
    if isinstance(deprecated, str):
        extra.append(f"deprecated: {deprecated}")
    if resolution.details.get("prerelease_only"):
        extra.append("only pre-releases are published")
    if resolution.stale:
        extra.append("from stale cache")
    return replace(finding, note="; ".join(extra))


def _evaluate_constraint(
    ref: DependencyRef, latest_raw: str, latest: Version, level: Bump, floors: bool = True
) -> Finding:
    if not ref.spec.strip():
        return Finding(ref, "unpinned", latest=latest_raw, note="no version constraint")
    try:
        constraint = _parse_constraint(ref.kind, ref.spec)
    except ConstraintError as exc:
        return Finding(ref, "skipped", latest=latest_raw, note=str(exc))
    if constraint.is_unbounded:
        return Finding(ref, "unpinned", latest=latest_raw, note="accepts any version")
    result = assess(constraint, latest, level=level, floors=floors)
    if not result.outdated:
        return Finding(ref, "ok", latest=latest_raw, bump=result.bump)
    new_spec = rewrite_spec(constraint, latest, fallback_op=_FALLBACK_OP.get(ref.kind, ">="))
    note = (
        "constraint excludes the latest release"
        if result.excludes_latest
        else ("floor is a breaking release behind")
    )
    return Finding(
        ref,
        "outdated",
        latest=latest_raw,
        suggested=ref.render(new_spec),
        bump=result.bump,
        note=note,
    )


def _evaluate_gha(ref: DependencyRef, res: Resolution, latest: Version, level: Bump) -> Finding:
    latest_raw = res.version or latest.raw
    current = parse_version(ref.spec)
    if current is None:
        return Finding(ref, "unpinned", latest=latest_raw, note=f"ref {ref.spec!r} is a branch")
    target = truncate(latest, current.precision)
    bump = bump_kind(current, target)
    if not meets_level(bump, level):
        return Finding(ref, "ok", latest=latest_raw, bump=bump)
    prefix = "v" if ref.spec[:1] in ("v", "V") else ""
    new_ref = prefix + target.raw
    if ref.sha:
        sha = res.details.get("sha")
        pin = sha if isinstance(sha, str) and sha else f"<commit SHA of {latest_raw}>"
        suggested = f"{ref.display_name}@{pin} # {latest_raw}"
        return Finding(ref, "outdated", latest=latest_raw, suggested=suggested, bump=bump)
    return Finding(ref, "outdated", latest=latest_raw, suggested=ref.render(new_ref), bump=bump)


def _eol_note(cycle: Cycle) -> str:
    when = cycle.eol_date()
    return f"{cycle.cycle} is EOL" + (f" since {when.isoformat()}" if when else "")


def _evaluate_cycle(
    ref: DependencyRef, res: Resolution, latest: Version, level: Bump, today: date
) -> Finding:
    cycles = cycles_from_details(res.details)
    rec_name = res.details.get("cycle")
    recommended = next((c for c in cycles if c.cycle == rec_name), None)
    current = parse_version(ref.spec)
    codename: Cycle | None = None
    if current is None or current.is_prerelease:
        codename = match_codename(cycles, ref.spec)
        if codename is None or codename.version is None:
            return Finding(ref, "unpinned", latest=res.version, note=f"tag {ref.spec!r} floats")
        current = codename.version
    cycle = codename or match_cycle(cycles, current)
    eol = cycle is not None and cycle.is_eol(today)

    if codename is not None:
        target = recommended.version if recommended and recommended.version else latest
        target = truncate(target, current.precision)
    else:
        target = truncate(latest, current.precision)
    bump = bump_kind(current, target)
    behind = meets_level(bump, level) and not ref.eol_only
    if not behind and not eol:
        return Finding(ref, "ok", latest=res.version, bump=bump)

    if bump is None and eol:
        # Ahead of the recommendation but still EOL (e.g. an expired interim release).
        newest = next((c for c in sort_cycles(cycles) if not c.is_eol(today)), None)
        target_cycle = newest or recommended
        target = (target_cycle.latest_version if target_cycle else None) or latest
        target = truncate(target, current.precision)
        bump = bump_kind(current, target) or "major"
    if codename is not None:
        new_cycle = recommended
        new_spec = (new_cycle.codename_slug if new_cycle else None) or target.raw
    else:
        new_spec = target.raw
    note = _eol_note(cycle) if eol and cycle is not None else ""
    if ref.name in DATABASE_PRODUCTS and bump == "major":
        migration = "major database upgrade: existing data needs a migration (dump/restore)"
        note = f"{note}; {migration}" if note else migration
    if ref.eol_only:
        note = f"{note}; drop it from the matrix and add {target.raw}" if note else ""
    return Finding(
        ref, "outdated", latest=res.version, suggested=ref.render(new_spec), bump=bump, note=note
    )


def _evaluate_runtime(ref: DependencyRef, res: Resolution, latest: Version, today: date) -> Finding:
    """A supported-runtime floor (``requires-python``) is outdated when it still targets an
    end-of-life release, or when it excludes the current release."""
    try:
        constraint = _parse_constraint("runtime", ref.spec)
    except ConstraintError as exc:
        return Finding(ref, "skipped", latest=res.version, note=str(exc))
    if constraint.is_unbounded:
        return Finding(ref, "unpinned", latest=res.version, note="accepts any version")
    cycles = [c for c in sort_cycles(cycles_from_details(res.details)) if c.is_released(today)]
    floor = constraint.floor
    floor_cycle = match_cycle(cycles, floor) if floor is not None else None
    known = [c.version for c in cycles if c.version is not None]
    eol = False
    if floor_cycle is not None:
        eol = floor_cycle.is_eol(today)
    elif floor is not None and known and floor < min(known):
        eol = True
    excluded = not constraint.allows(latest)
    if not eol and not excluded:
        return Finding(ref, "ok", latest=res.version)
    supported = [c for c in cycles if not c.is_eol(today) and c.version is not None]
    oldest = min(supported, key=lambda c: c.version or latest) if supported else None
    target = oldest.version if oldest and oldest.version else truncate(latest, 2)
    notes = []
    if eol:
        notes.append(_eol_note(floor_cycle) if floor_cycle else f"{floor} is EOL")
    if excluded:
        notes.append(f"excludes the current release {res.version}")
    if excluded and not eol and floor is not None and constraint.clauses == 1:
        # A deliberate single-line pin ("==3.14.*") moves forward instead of widening.
        new_spec = rewrite_spec(constraint, latest, fallback_op=">=")
    elif floor is not None and constraint.clauses == 1 and not excluded:
        new_spec = rewrite_spec(constraint, target, fallback_op=">=", keep_precision=False)
    else:
        new_spec = f">={target.raw}"
    bump = bump_kind(floor, target) if floor is not None else None
    return Finding(
        ref,
        "outdated",
        latest=res.version,
        suggested=ref.render(new_spec),
        bump=bump or "minor",
        note="; ".join(notes),
    )
