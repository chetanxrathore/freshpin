"""``Cargo.toml``: ``[dependencies]``, ``[dev-dependencies]``, ``[build-dependencies]``,
``[workspace.dependencies]``, target-specific tables and ``[dependencies.<crate>]``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from freshpin.auditors.base import (
    DependencyRef,
    honour_ignore,
    strip_comment,
    unquote,
    with_status,
)

_HEADER_RE = re.compile(r"^\[\s*([^\[\]]+?)\s*\]$")
_KEY_RE = re.compile(r"""^("(?:[^"\\]|\\.)*"|'[^']*'|[A-Za-z0-9_\-]+)\s*=\s*(.*)$""")
_FIELD_RE = re.compile(r"""(\w[\w-]*)\s*=\s*("(?:[^"\\]|\\.)*"|'[^']*'|\[[^\]]*\]|[^,}\s]+)""")
_KINDS = ("dependencies", "dev-dependencies", "build-dependencies")
_NON_REGISTRY = ("path", "git", "workspace", "registry")


def _split_table(name: str) -> list[str]:
    parts = re.findall(r"""\s*("(?:[^"\\]|\\.)*"|'[^']*'|[^.]+)\s*(?:\.|$)""", name)
    return [unquote(p) for p in parts if p.strip()]


def classify_table(name: str) -> tuple[str | None, str | None]:
    """Return ``(section, crate)`` for dependency tables.

    ``dependencies`` -> ("dependencies", None); ``dev-dependencies.serde`` ->
    ("dev-dependencies", "serde"); ``target.'cfg(unix)'.dependencies`` ->
    ("target.cfg(unix).dependencies", None); anything else -> (None, None).
    """
    parts = _split_table(name)
    if parts[:1] == ["workspace"] and parts[1:2] == ["dependencies"]:
        parts = parts[1:]
        prefix = "workspace."
    elif parts[:1] == ["target"] and len(parts) >= 3:
        prefix = f"target.{parts[1]}."
        parts = parts[2:]
    else:
        prefix = ""
    if parts and parts[0] in _KINDS:
        section = prefix + parts[0]
        if len(parts) == 1:
            return section, None
        if len(parts) == 2:
            return section, parts[1]
    return None, None


def _fields(text: str) -> dict[str, str]:
    inner = text.strip()
    if inner.startswith("{"):
        inner = inner[1 : inner.rfind("}") if "}" in inner else None]
    return {m.group(1): unquote(m.group(2)) for m in _FIELD_RE.finditer(inner)}


@dataclass
class _Pending:
    crate: str
    section: str
    header_line: int
    raw: str
    version: str | None = None
    version_line: int = 0
    package: str | None = None
    source: str | None = None


def _make_ref(
    path: Path,
    line: int,
    alias: str,
    package: str | None,
    spec: str,
    section: str,
    template: str,
    raw: str,
    source: str | None,
) -> DependencyRef:
    ref = DependencyRef(
        file=path,
        line=line,
        ecosystem="crates",
        name=(package or alias).strip(),
        spec=spec,
        kind="cargo",
        display_name=alias,
        template=template,
        section=section,
    )
    if source:
        return with_status(ref, "skipped", f"{source} dependency")
    return honour_ignore(ref, raw)


def parse_cargo_toml(path: Path, text: str) -> list[DependencyRef]:
    refs: list[DependencyRef] = []
    section: str | None = None
    pending: _Pending | None = None

    def close() -> None:
        nonlocal pending
        if pending is not None and (pending.version is not None or pending.source):
            refs.append(
                _make_ref(
                    path,
                    pending.version_line or pending.header_line,
                    pending.crate,
                    pending.package,
                    pending.version or "",
                    pending.section,
                    f'[{pending.section}.{pending.crate}] version = "{{spec}}"',
                    pending.raw,
                    pending.source,
                )
            )
        pending = None

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = strip_comment(raw).strip()
        if not line:
            continue
        header = _HEADER_RE.match(line)
        if header:
            close()
            sec, crate = classify_table(header.group(1))
            section = sec if crate is None else None
            if sec and crate:
                pending = _Pending(crate=crate, section=sec, header_line=lineno, raw=raw)
            continue
        kv = _KEY_RE.match(line)
        if not kv:
            continue
        key, value = unquote(kv.group(1)), kv.group(2).strip()
        if pending is not None:
            if key == "version":
                pending.version, pending.version_line = unquote(value), lineno
                pending.raw += raw
            elif key == "package":
                pending.package = unquote(value)
            elif key in _NON_REGISTRY:
                pending.source = key
            continue
        if section is None:
            continue
        if value.startswith("{"):
            fields = _fields(value)
            source = next((k for k in _NON_REGISTRY if k in fields), None)
            if "version" not in fields and not source:
                continue
            refs.append(
                _make_ref(
                    path,
                    lineno,
                    key,
                    fields.get("package"),
                    fields.get("version", ""),
                    section,
                    f"{key} = {{ {source} = ... }}"
                    if source
                    else f'{key} = {{ version = "{{spec}}" }}',
                    raw,
                    source,
                )
            )
        elif value[:1] in ('"', "'"):
            refs.append(
                _make_ref(
                    path,
                    lineno,
                    key,
                    None,
                    unquote(value),
                    section,
                    f'{key} = "{{spec}}"',
                    raw,
                    None,
                )
            )
    close()
    return refs
