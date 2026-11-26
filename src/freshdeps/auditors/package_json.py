"""``package.json``: dependencies, devDependencies, optional/peer deps and ``engines.node``."""

from __future__ import annotations

import json
import re
from pathlib import Path

from freshdeps.auditors.base import DependencyRef, with_status

SECTIONS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
_NON_REGISTRY_PREFIXES = (
    "file:",
    "link:",
    "workspace:",
    "portal:",
    "patch:",
    "catalog:",
    "git:",
    "git+",
    "github:",
    "gitlab:",
    "bitbucket:",
    "http:",
    "https:",
    "jsr:",
)
_DIST_TAG_RE = re.compile(r"^[A-Za-z][\w.-]*$")


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _find_key(text: str, key: str, start: int) -> int:
    m = re.compile(rf'"{re.escape(key)}"\s*:').search(text, start)
    return m.start() if m else start


def _section_start(text: str, section: str) -> int:
    m = re.search(rf'"{re.escape(section)}"\s*:\s*\{{', text)
    return m.end() if m else 0


def _dependency(path: Path, line: int, section: str, key: str, spec: str) -> DependencyRef:
    name, value, template = key, spec.strip(), f'"{key}": "{{spec}}"'
    if value.startswith("npm:"):
        # Aliases: "my-react": "npm:react@^18.2.0"
        target = value[4:]
        at = target.rfind("@")
        if at > 0:
            name, value = target[:at], target[at + 1 :]
            template = f'"{key}": "npm:{name}@{{spec}}"'
        else:
            name, value = target, ""
    ref = DependencyRef(
        file=path,
        line=line,
        ecosystem="npm",
        name=name,
        spec=value,
        kind="npm",
        display_name=key,
        template=template,
        section=section,
    )
    if value.startswith(_NON_REGISTRY_PREFIXES) or ("/" in value and not value.startswith("@")):
        return with_status(ref, "skipped", "not a registry version")
    if _DIST_TAG_RE.match(value) and value not in ("x", "X", "latest"):
        return with_status(ref, "unpinned", f"dist-tag {value!r}")
    return ref


def parse_package_json(path: Path, text: str) -> list[DependencyRef]:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("package.json is not a JSON object")
    refs: list[DependencyRef] = []
    for section in SECTIONS:
        deps = data.get(section)
        if not isinstance(deps, dict):
            continue
        start = _section_start(text, section)
        for key, spec in deps.items():
            if not isinstance(key, str) or not isinstance(spec, str):
                continue
            line = _line_of(text, _find_key(text, key, start))
            refs.append(_dependency(path, line, section, key, spec))
    engines = data.get("engines")
    if isinstance(engines, dict) and isinstance(engines.get("node"), str):
        start = _section_start(text, "engines")
        refs.append(
            DependencyRef(
                file=path,
                line=_line_of(text, _find_key(text, "node", start)),
                ecosystem="eol",
                name="nodejs",
                spec=engines["node"],
                kind="runtime",
                display_name="engines.node",
                template='"node": "{spec}"',
                section="engines",
                syntax="semver",
            )
        )
    return refs
