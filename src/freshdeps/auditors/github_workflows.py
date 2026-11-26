"""GitHub workflows (``.github/workflows/*.yml``) and composite ``action.yml`` files.

Detects:

* ``uses: owner/repo[/path]@ref`` - including SHA pins with a ``# vX.Y.Z`` comment
* ``uses: docker://image:tag``, ``container: image:tag`` and service ``image:`` lines
* ``python-version`` / ``node-version`` / ``go-version`` pins for setup-* actions
"""

from __future__ import annotations

import re
from pathlib import Path

from freshdeps.auditors.base import DependencyRef, honour_ignore, unquote, with_status
from freshdeps.auditors.docker import IMAGE_LINE_RE, image_ref
from freshdeps.versioning import parse_version

_USES_RE = re.compile(r"""^\s*(?:-\s+)?uses\s*:\s*(['"]?)([^'"\s#]+)\1\s*(?:#\s*(.*))?$""")
_CONTAINER_RE = re.compile(r"""^\s*(?:-\s+)?container\s*:\s*(['"]?)([^'"\s#{}]+)\1\s*(?:#.*)?$""")
_SETUP_RE = re.compile(r"""^\s*(?:-\s+)?(python|node|go)-version\s*:\s*(.+?)\s*$""")
_SHA_RE = re.compile(r"^(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}$|^[0-9a-f]{40}$")
_COMMENT_VERSION_RE = re.compile(r"\bv?\d+(?:\.\d+)*\b")
_PIN_RE = re.compile(r"^(\d+(?:\.\d+)*)(\.x)?$")
_SETUP_PRODUCTS = {"python": "python", "node": "nodejs", "go": "go"}


def _uses_ref(path: Path, line: int, value: str, comment: str, raw: str) -> DependencyRef | None:
    if value.startswith(("./", "../", "${{")):
        return None
    if value.startswith("docker://"):
        return image_ref(path, line, value[len("docker://") :], raw_line=raw, section="uses")
    if "@" not in value:
        return None
    action, _, ref = value.rpartition("@")
    repo = "/".join(action.split("/")[:2]).lower()
    base = DependencyRef(
        file=path,
        line=line,
        ecosystem="gha",
        name=repo,
        spec=ref,
        kind="gha",
        display_name=action,
        template=f"{action}@{{spec}}",
        section="uses",
    )
    if _SHA_RE.match(ref):
        version = _COMMENT_VERSION_RE.search(comment)
        if version is None:
            return with_status(
                base, "skipped", "pinned by commit SHA (add a '# vX.Y.Z' comment to track it)"
            )
        return honour_ignore(
            DependencyRef(
                file=path,
                line=line,
                ecosystem="gha",
                name=repo,
                spec=version.group(0),
                kind="gha",
                display_name=action,
                template=f"{action}@{ref} # {{spec}}",
                section="uses",
                sha=ref,
            ),
            raw,
        )
    if parse_version(ref) is None:
        return with_status(base, "unpinned", f"ref {ref!r} is a branch or non-version tag")
    return honour_ignore(base, raw)


def _setup_refs(path: Path, line: int, tool: str, value: str, raw: str) -> list[DependencyRef]:
    key = f"{tool}-version"
    product = _SETUP_PRODUCTS[tool]
    value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    if value.startswith("${{"):
        return []
    if value.startswith("["):
        refs = []
        for item in re.findall(r"""['"]?([^,'"\[\]\s]+)['"]?""", value):
            m = _PIN_RE.match(item)
            if m and not m.group(2):
                ref = DependencyRef(
                    file=path,
                    line=line,
                    ecosystem="eol",
                    name=product,
                    spec=m.group(1),
                    kind="cycle",
                    display_name=key,
                    template=f'{key}: [... "{{spec}}" ...]',
                    section="matrix",
                    eol_only=True,
                )
                refs.append(honour_ignore(ref, raw))
        return refs
    quote = value[0] if value[:1] in ("'", '"') else ""
    inner = unquote(value)
    m = _PIN_RE.match(inner)
    if not m:
        return []
    note = ""
    if not quote and "." in inner and inner.endswith("0") and not m.group(2):
        note = f"unquoted YAML reads {inner} as a number ({float(inner):g}) - quote it"
    ref = DependencyRef(
        file=path,
        line=line,
        ecosystem="eol",
        name=product,
        spec=m.group(1),
        kind="cycle",
        display_name=key,
        template=f"{key}: {quote}{{spec}}{m.group(2) or ''}{quote}",
        section="with",
        note=note,
    )
    return [honour_ignore(ref, raw)]


def parse_workflow(path: Path, text: str) -> list[DependencyRef]:
    refs: list[DependencyRef] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if raw.lstrip().startswith("#"):
            continue
        uses = _USES_RE.match(raw)
        if uses:
            ref = _uses_ref(path, lineno, uses.group(2), uses.group(3) or "", raw)
            if ref is not None:
                refs.append(ref)
            continue
        image = IMAGE_LINE_RE.match(raw) or _CONTAINER_RE.match(raw)
        if image:
            img = image_ref(path, lineno, image.group(2), raw_line=raw, section="container")
            if img is not None:
                refs.append(img)
            continue
        setup = _SETUP_RE.match(raw)
        if setup:
            refs.extend(_setup_refs(path, lineno, setup.group(1), setup.group(2), raw))
    return refs
