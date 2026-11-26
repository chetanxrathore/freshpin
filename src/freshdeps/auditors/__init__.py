"""File discovery, parsing dispatch and the ``audit`` orchestration."""

from __future__ import annotations

import fnmatch
import os
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from freshdeps.auditors.base import (
    STATUSES,
    DependencyRef,
    Finding,
    display_path,
    evaluate,
    with_status,
)
from freshdeps.auditors.cargo_toml import parse_cargo_toml
from freshdeps.auditors.docker import parse_compose, parse_dockerfile
from freshdeps.auditors.github_workflows import parse_workflow
from freshdeps.auditors.package_json import parse_package_json
from freshdeps.auditors.pyproject import parse_pyproject, parse_requirements
from freshdeps.registries import Resolver
from freshdeps.versioning import Bump

__all__ = [
    "AuditReport",
    "DependencyRef",
    "Finding",
    "audit_paths",
    "detect_kind",
    "discover_files",
    "parse_file",
]

Parser = Callable[[Path, str], list[DependencyRef]]
PARSERS: dict[str, Parser] = {
    "pyproject": parse_pyproject,
    "requirements": parse_requirements,
    "package_json": parse_package_json,
    "cargo": parse_cargo_toml,
    "workflow": parse_workflow,
    "dockerfile": parse_dockerfile,
    "compose": parse_compose,
}

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "bower_components",
    ".venv",
    "venv",
    ".tox",
    ".nox",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "site-packages",
    "target",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".turbo",
    ".terraform",
    ".cache",
}

_REQUIREMENTS_RE = re.compile(r"^(?:.*[-_.])?requirements(?:[-_.].*)?\.(?:txt|in)$")
_COMPOSE_RE = re.compile(r"^(?:docker-)?compose(?:[-_.][\w.-]*)?\.ya?ml$")


def detect_kind(path: Path) -> str | None:
    """Which auditor understands ``path`` (by file name), or ``None``."""
    name = path.name
    lower = name.lower()
    if name == "pyproject.toml":
        return "pyproject"
    if name == "package.json":
        return "package_json"
    if name == "Cargo.toml":
        return "cargo"
    if _REQUIREMENTS_RE.match(lower) or (
        path.parent.name == "requirements" and lower.endswith((".txt", ".in"))
    ):
        return "requirements"
    if lower.endswith((".yml", ".yaml")):
        parts = [p.lower() for p in path.parts]
        for i in range(len(parts) - 2):
            if parts[i] == ".github" and parts[i + 1] == "workflows":
                return "workflow"
        if lower in ("action.yml", "action.yaml"):
            return "workflow"
        if _COMPOSE_RE.match(lower):
            return "compose"
        return None
    if (
        lower in ("dockerfile", "containerfile")
        or lower.startswith(("dockerfile.", "containerfile."))
        or lower.endswith((".dockerfile", ".containerfile"))
    ):
        return "dockerfile"
    return None


def _excluded(rel: str, patterns: Sequence[str]) -> bool:
    return any(
        fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p.rstrip("/") + "/*") for p in patterns
    )


def discover_files(root: Path, *, exclude: Sequence[str] = ()) -> list[Path]:
    """Recursively find every supported file under ``root`` (skipping vendored dirs)."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in SKIP_DIRS
            and not d.endswith(".egg-info")
            and not _excluded((current / d).relative_to(root).as_posix(), exclude)
        )
        for filename in sorted(filenames):
            path = current / filename
            rel = path.relative_to(root).as_posix()
            if detect_kind(path) and not _excluded(rel, exclude):
                found.append(path)
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def parse_file(path: Path, kind: str | None = None) -> list[DependencyRef]:
    kind = kind or detect_kind(path)
    if kind is None:
        raise ValueError(f"unsupported file type: {path.name}")
    text = path.read_text(encoding="utf-8")
    return PARSERS[kind](path, text)


@dataclass
class AuditReport:
    files: list[Path] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    base: Path | None = None

    @property
    def outdated(self) -> list[Finding]:
        return [f for f in self.findings if f.status == "outdated"]

    def counts(self) -> dict[str, int]:
        tally = Counter(f.status for f in self.findings)
        return {status: tally.get(status, 0) for status in STATUSES}

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": {
                "files": len(self.files),
                "checked": len(self.findings),
                **self.counts(),
            },
            "files": [display_path(p, self.base) for p in self.files],
            "findings": [f.to_dict(self.base) for f in self.findings],
            "errors": self.errors,
        }


def _matches_any(ref: DependencyRef, names: Iterable[str]) -> bool:
    candidates = {ref.name.lower(), ref.display_name.lower(), f"{ref.ecosystem}:{ref.name}".lower()}
    return any(fnmatch.fnmatch(c, pat.lower()) for pat in names for c in candidates)


def audit_paths(
    paths: Sequence[Path],
    resolver: Resolver,
    *,
    level: Bump = "minor",
    ignore: Sequence[str] = (),
    exclude: Sequence[str] = (),
    today: date | None = None,
    base: Path | None = None,
    line_filter: Callable[[DependencyRef], bool] | None = None,
) -> AuditReport:
    """Discover, parse, resolve (concurrently) and evaluate every declaration."""
    report = AuditReport(base=base)
    for path in paths:
        if path.is_dir():
            report.files.extend(discover_files(path, exclude=exclude))
        elif path.is_file():
            if detect_kind(path) is None:
                report.errors.append(f"{display_path(path, base)}: unsupported file type")
            else:
                report.files.append(path)
        else:
            report.errors.append(f"{display_path(path, base)}: no such file or directory")
    refs: list[DependencyRef] = []
    for file in report.files:
        try:
            parsed = parse_file(file)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            report.errors.append(f"{display_path(file, base)}: {exc}")
            continue
        for ref in sorted(parsed, key=lambda r: r.line):
            if line_filter is not None and not line_filter(ref):
                continue
            if ignore and _matches_any(ref, ignore):
                ref = with_status(ref, "skipped", "ignored (--ignore)")
            refs.append(ref)
    lookups = [(r.ecosystem, r.name) for r in refs if r.needs_lookup]
    resolutions = resolver.resolve_many(lookups) if lookups else {}
    report.findings = [
        evaluate(ref, resolutions.get((ref.ecosystem, ref.name)), level=level, today=today)
        for ref in refs
    ]
    return report
