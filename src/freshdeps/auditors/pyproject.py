"""``pyproject.toml`` (PEP 621, PEP 735, build-system, uv, Poetry) and ``requirements*.txt``.

A line-oriented scanner is used instead of ``tomllib`` so every finding carries the
exact line number (and so it works on Python 3.10, which has no ``tomllib``).
"""

from __future__ import annotations

import re
from pathlib import Path

from freshdeps.auditors.base import (
    DependencyRef,
    bracket_delta,
    honour_ignore,
    quoted_strings,
    strip_comment,
    unquote,
    with_status,
)
from freshdeps.registries.pypi import PyPIRegistry

_HEADER_RE = re.compile(r"^\[\[?\s*([^\[\]]+?)\s*\]\]?$")
_KEY_RE = re.compile(r"""^("(?:[^"\\]|\\.)*"|'[^']*'|[A-Za-z0-9_.\-]+)\s*=\s*(.*)$""")
_REQ_RE = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(\[[^\]]*\])?\s*(.*)$")
_POETRY_TABLE_RE = re.compile(
    r"^tool\.poetry\.(?:dependencies|dev-dependencies|group\.[^.]+\.dependencies)$"
)
_INLINE_FIELD_RE = re.compile(r"""(\w[\w-]*)\s*=\s*("(?:[^"\\]|\\.)*"|'[^']*'|[^,}\s]+)""")
_PYPI = PyPIRegistry()


def table_name(header: str) -> str:
    """Normalise ``[ tool . "poetry" ]`` -> ``tool.poetry``."""
    parts = re.findall(r"""\s*("(?:[^"\\]|\\.)*"|'[^']*'|[^.]+)\s*(?:\.|$)""", header)
    return ".".join(unquote(p) for p in parts if p.strip())


def parse_requirement(text: str) -> tuple[str, str, str, bool] | None:
    """Split a PEP 508 string into ``(name, extras, specifier, is_direct_url)``."""
    body = text.split(";", 1)[0].strip()
    m = _REQ_RE.match(body)
    if not m:
        return None
    name, extras, rest = m.group(1), m.group(2) or "", m.group(3).strip()
    if rest.startswith("@"):
        return name, extras, "", True
    return name, extras, rest.strip("() ").strip(), False


def _pep508_ref(
    path: Path, line: int, text: str, section: str, raw_line: str
) -> DependencyRef | None:
    parsed = parse_requirement(text)
    if parsed is None:
        return None
    name, extras, spec, direct = parsed
    ref = DependencyRef(
        file=path,
        line=line,
        ecosystem="pypi",
        name=_PYPI.normalize(name),
        spec=spec,
        kind="pep508",
        display_name=name,
        template=f"{name}{extras}{{spec}}",
        section=section,
    )
    if direct:
        return with_status(ref, "skipped", "direct URL reference")
    return honour_ignore(ref, raw_line)


def _inline_fields(text: str) -> dict[str, str]:
    inner = text.strip()
    if inner.startswith("{"):
        inner = inner[1 : inner.rfind("}") if "}" in inner else None]
    return {m.group(1): unquote(m.group(2)) for m in _INLINE_FIELD_RE.finditer(inner)}


def _poetry_ref(
    path: Path, line: int, key: str, value: str, section: str, raw_line: str
) -> DependencyRef | None:
    value = value.strip()
    if value.startswith("["):
        return None  # multiple-constraint dependencies are out of scope
    if value.startswith("{"):
        fields = _inline_fields(value)
        spec = fields.get("version", "")
        template = f'{key} = {{ version = "{{spec}}" }}'
        skip = next((k for k in ("path", "git", "url", "develop") if k in fields), None)
    else:
        spec, template, skip = unquote(value), f'{key} = "{{spec}}"', None
    if key.lower() == "python":
        ref = DependencyRef(
            file=path,
            line=line,
            ecosystem="eol",
            name="python",
            spec=spec,
            kind="runtime",
            display_name="python",
            template=template,
            section=section,
            syntax="semver",
        )
    else:
        ref = DependencyRef(
            file=path,
            line=line,
            ecosystem="pypi",
            name=_PYPI.normalize(key),
            spec=spec,
            kind="poetry",
            display_name=key,
            template=template,
            section=section,
        )
    if skip:
        return with_status(ref, "skipped", f"{skip} dependency")
    return honour_ignore(ref, raw_line)


def _pep508_section(table: str, key: str) -> str | None:
    if table == "project" and key == "dependencies":
        return "dependencies"
    if table == "project.optional-dependencies":
        return f"optional-dependencies.{key}"
    if table == "dependency-groups":
        return f"dependency-groups.{key}"
    if table == "build-system" and key == "requires":
        return "build-system.requires"
    if table == "tool.uv" and key == "dev-dependencies":
        return "tool.uv.dev-dependencies"
    return None


def parse_pyproject(path: Path, text: str) -> list[DependencyRef]:
    refs: list[DependencyRef] = []
    table = ""
    array_section: str | None = None  # None = not in an array; "" = untracked array
    in_array = False
    depth = 0
    in_multiline = False

    def add_strings(segment: str, lineno: int, raw: str, section: str) -> None:
        for item in quoted_strings(segment):
            ref = _pep508_ref(path, lineno, item, section, raw)
            if ref is not None:
                refs.append(ref)

    for lineno, raw in enumerate(text.splitlines(), start=1):
        if in_multiline:
            if raw.count('"""') % 2 == 1 or raw.count("'''") % 2 == 1:
                in_multiline = False
            continue
        line = strip_comment(raw).strip()
        if in_array:
            if array_section:
                add_strings(line, lineno, raw, array_section)
            depth += bracket_delta(line)
            if depth <= 0:
                in_array = False
            continue
        if not line:
            continue
        header = _HEADER_RE.match(line)
        if header:
            table = table_name(header.group(1))
            continue
        kv = _KEY_RE.match(line)
        if not kv:
            continue
        key, value = unquote(kv.group(1)), kv.group(2).strip()
        if value.count('"""') % 2 == 1 or value.count("'''") % 2 == 1:
            in_multiline = True
            continue
        if value.startswith("["):
            section = _pep508_section(table, key)
            if section:
                add_strings(value, lineno, raw, section)
            depth = bracket_delta(value)
            if depth > 0:
                in_array, array_section = True, section or ""
            continue
        if table == "project" and key == "requires-python":
            ref = DependencyRef(
                file=path,
                line=lineno,
                ecosystem="eol",
                name="python",
                spec=unquote(value),
                kind="runtime",
                display_name="requires-python",
                template='requires-python = "{spec}"',
                section="project",
                syntax="pep440",
            )
            refs.append(honour_ignore(ref, raw))
        elif _POETRY_TABLE_RE.match(table):
            poetry = _poetry_ref(path, lineno, key, value, table, raw)
            if poetry is not None:
                refs.append(poetry)
    return refs


def parse_requirements(path: Path, text: str) -> list[DependencyRef]:
    refs: list[DependencyRef] = []
    pending = ""
    start = 0
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not pending:
            start = lineno
        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        full = pending + raw
        pending = ""
        line = re.split(r"(?:^|\s)#", full, maxsplit=1)[0].strip()
        if not line or line.startswith("-"):
            continue  # options: -r, -c, -e, --index-url ...
        line = re.split(r"\s+--?[a-z]", line, maxsplit=1)[0]  # trailing --hash=...
        if "://" in line.split("@", 1)[0] or line.startswith((".", "/", "~")):
            continue  # bare URLs and local paths
        ref = _pep508_ref(path, start, line, "requirements", full)
        if ref is not None:
            refs.append(ref)
    return refs
