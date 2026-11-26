"""File discovery, parsing dispatch and the ``audit`` orchestration."""

from __future__ import annotations

import fnmatch
import os
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

from freshpin.auditors.base import (
    STATUSES,
    DependencyRef,
    Finding,
    display_path,
    evaluate,
    with_status,
)
from freshpin.auditors.cargo_toml import parse_cargo_toml
from freshpin.auditors.docker import parse_compose, parse_dockerfile
from freshpin.auditors.github_workflows import parse_workflow
from freshpin.auditors.package_json import parse_package_json
from freshpin.auditors.pyproject import parse_pyproject, parse_requirements
from freshpin.registries import Resolver
from freshpin.registries.dockerhub import TagChecker
from freshpin.registries.endoflife import cycles_from_details, sort_cycles
from freshpin.versioning import Bump, parse_version, truncate

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
    text = path.read_text(encoding="utf-8-sig")  # tolerate a UTF-8 byte-order mark
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
    verify_tags: bool = True,
    check_floors: bool = True,
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
        evaluate(
            ref,
            resolutions.get((ref.ecosystem, ref.name)),
            level=level,
            today=today,
            floors=check_floors,
        )
        for ref in refs
    ]
    if verify_tags:
        checker = TagChecker(resolver.http, resolver.cache, offline=resolver.offline)
        report.findings = verify_docker_suggestions(
            report.findings, checker, OsVariants(resolver, today or date.today())
        )
        resolver.cache.flush()
    return report


class OsVariants:
    """Current replacements for OS-specific Docker tag suffixes, from endoflife.date.

    ``-buster``/``-bullseye`` -> ``-trixie``/``-bookworm``; ``-focal`` -> ``-noble``...;
    ``-alpine3.14`` -> ``-alpine3.24`` or ``-alpine``. Looked up lazily, once.
    """

    def __init__(self, resolver: Resolver, today: date) -> None:
        self.resolver = resolver
        self.today = today
        self._families: list[tuple[set[str], list[str]]] | None = None

    def _load(self) -> list[tuple[set[str], list[str]]]:
        if self._families is None:
            found = self.resolver.resolve_many(
                [("eol", "debian"), ("eol", "ubuntu"), ("eol", "alpine-linux")]
            )
            families: list[tuple[set[str], list[str]]] = []
            for product in ("debian", "ubuntu"):
                res = found[("eol", product)]
                cycles = sort_cycles(cycles_from_details(res.details))
                known = {c.codename_slug for c in cycles if c.codename_slug}
                current = [
                    slug
                    for c in cycles
                    if (slug := c.codename_slug)
                    and c.is_released(self.today)
                    and not c.is_eol(self.today)
                    and (product == "debian" or c.is_lts(self.today))
                ]
                families.append((known, current[:2]))
            alpine = found[("eol", "alpine-linux")]
            cycles = sort_cycles(cycles_from_details(alpine.details))
            current_alpine = [f"alpine{c.cycle}" for c in cycles if not c.is_eol(self.today)]
            families.append(({"alpine"}, [*current_alpine[:1], "alpine"]))
            self._families = families
        return self._families

    def alternatives(self, suffix: str) -> list[str]:
        """Rewrites of ``suffix`` that swap an old OS release for a current one."""
        out: list[str] = []
        for known, current in self._load():
            for token in re.findall(r"[a-z]+[0-9.]*", suffix):
                base = re.sub(r"[0-9.]+$", "", token) if token.startswith("alpine") else token
                if base not in known and not (base == "alpine" and "alpine" in known):
                    continue
                for replacement in current:
                    candidate = suffix.replace(token, replacement, 1)
                    if candidate != suffix and candidate not in out:
                        out.append(candidate)
        return out


def _tag_of(image: str, rendered: str) -> str:
    """``postgres:19-alpine@sha256:x`` with image ``postgres`` -> ``19-alpine``."""
    return rendered[len(image) + 1 :].split("@", 1)[0] if rendered.startswith(image + ":") else ""


def _version_forms(value: str, latest: str | None) -> list[str]:
    """Spellings of the target version to try, most precise first (12.3.4, 12.3, 12)."""
    forms = [value]
    parsed = parse_version(latest) if latest else None
    if latest:
        forms.append(latest)
    if parsed is not None:
        forms += [truncate(parsed, n).raw for n in range(parsed.precision - 1, 0, -1)]
    return [f for f in dict.fromkeys(forms) if f]


def _verify_one(finding: Finding, checker: TagChecker, os_variants: OsVariants | None) -> Finding:
    ref = finding.ref
    if finding.status != "outdated" or not ref.image or not finding.suggested:
        return finding
    tag = _tag_of(ref.image, finding.suggested)
    exists = checker.exists(ref.image, tag)
    if exists is not False:
        return replace(finding, verified=exists)
    # The short tag is not published (e.g. elasticsearch:9.5): try the full version.
    if finding.latest:
        alternative = ref.render(finding.latest)
        alt_tag = _tag_of(ref.image, alternative)
        if alt_tag and alt_tag != tag and checker.exists(ref.image, alt_tag):
            note = f"{ref.image}:{tag} is not published; use the full version tag"
            return replace(
                finding,
                suggested=alternative,
                verified=True,
                note="; ".join(n for n in (finding.note, note) if n),
            )
    # The OS variant in the suffix may be retired (python:3.15-buster): try current ones.
    head, _, tail = ref.template.partition("{spec}")
    suffix, at, digest = tail.partition("@")
    if os_variants is not None and finding.suggested.startswith(head):
        value = finding.suggested[len(head) : len(finding.suggested) - len(tail)]
        for alt_suffix in os_variants.alternatives(suffix):
            for version in _version_forms(value, finding.latest):
                candidate = f"{head}{version}{alt_suffix}{at}{digest}"
                alt_tag = _tag_of(ref.image, candidate)
                if checker.exists(ref.image, alt_tag):
                    old, new = suffix.lstrip("-"), alt_suffix.lstrip("-")
                    note = f"{old} variant is not published for {value}; using {new}"
                    return replace(
                        finding,
                        suggested=candidate,
                        verified=True,
                        note="; ".join(n for n in (finding.note, note) if n),
                    )
    note = f"{ref.image}:{tag} not found on Docker Hub - pick an available tag"
    return replace(finding, verified=False, note="; ".join(n for n in (finding.note, note) if n))


def verify_docker_suggestions(
    findings: list[Finding], checker: TagChecker, os_variants: OsVariants | None = None
) -> list[Finding]:
    """Confirm suggested Docker Hub tags exist (concurrently); repair or flag the rest."""
    todo = [i for i, f in enumerate(findings) if f.status == "outdated" and f.ref.image]
    if not todo:
        return findings
    out = list(findings)
    with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
        checked = list(pool.map(lambda i: _verify_one(findings[i], checker, os_variants), todo))
    for i, verified in zip(todo, checked):
        out[i] = verified
    return out
