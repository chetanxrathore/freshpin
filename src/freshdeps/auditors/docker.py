"""``Dockerfile`` (``FROM``, with ``ARG`` substitution) and Compose files (``image:``)."""

from __future__ import annotations

import re
from pathlib import Path

from freshdeps.auditors.base import DependencyRef, honour_ignore, with_status
from freshdeps.registries.endoflife import docker_product, normalize_image_repository

_ARG_RE = re.compile(r"^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s*=\s*(\S+))?", re.IGNORECASE)
_FROM_RE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE)
IMAGE_LINE_RE = re.compile(r"""^\s*(?:-\s+)?image\s*:\s*(['"]?)([^'"\s#]+)\1""")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_TAG_VERSION_RE = re.compile(r"^(v?)(\d+(?:\.\d+)*)(.*)$")
FLOATING_TAGS = {
    "latest",
    "stable",
    "lts",
    "current",
    "edge",
    "mainline",
    "rolling",
    "nightly",
    "devel",
    "testing",
    "unstable",
    "sid",
    "alpine",
    "slim",
}


def substitute(text: str, values: dict[str, str]) -> str:
    """Expand ``${VAR}``, ``${VAR:-default}`` and ``$VAR`` (unknown vars are left as-is)."""

    def repl(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(3)
        if name in values:
            return values[name]
        if m.group(2) is not None:
            return m.group(2)
        return m.group(0)

    return _VAR_RE.sub(repl, text)


def split_image(image: str) -> tuple[str, str | None, str | None]:
    """``reg:5000/ns/name:tag@sha256:x`` -> ``("reg:5000/ns/name", "tag", "sha256:x")``."""
    ref, _, digest = image.partition("@")
    slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon > slash:
        return ref[:colon], ref[colon + 1 :], digest or None
    return ref, None, digest or None


def image_ref(
    path: Path, line: int, image: str, *, raw_line: str = "", section: str = "", note: str = ""
) -> DependencyRef | None:
    """Build a reference for a Docker image, or ``None`` if it is not worth reporting."""
    if "$" in image:
        return None  # unresolved variable
    repository, tag, digest = split_image(image)
    product = docker_product(repository)
    display = normalize_image_repository(repository) if product else repository
    base = DependencyRef(
        file=path,
        line=line,
        ecosystem="eol",
        name=product or display,
        spec=tag or "",
        kind="cycle",
        display_name=display,
        template=f"{repository}:{{spec}}" if tag else repository,
        section=section,
        note=note,
    )
    if product is None:
        return with_status(base, "skipped", "image not tracked by endoflife.date")
    if tag is None:
        return with_status(base, "unpinned", "no tag (implicitly :latest)")
    lowered = tag.lower()
    if lowered in FLOATING_TAGS or lowered.split("-", 1)[0] in FLOATING_TAGS:
        return with_status(base, "unpinned", f"floating tag {tag!r}")
    digest_note = "pinned by digest - update the digest too" if digest else ""
    m = _TAG_VERSION_RE.match(tag)
    if m:
        vprefix, version, rest = m.groups()
        template = f"{repository}:{vprefix}{{spec}}{rest}"
        spec = version
    else:
        first, sep, rest = tag.partition("-")  # codename tags: bookworm-slim, jammy
        template = f"{repository}:{{spec}}{sep}{rest}"
        spec = first
    if digest:
        template += f"@{digest}"
    ref = DependencyRef(
        file=path,
        line=line,
        ecosystem="eol",
        name=product,
        spec=spec,
        kind="cycle",
        display_name=display,
        template=template,
        section=section,
        note="; ".join(n for n in (note, digest_note) if n),
    )
    return honour_ignore(ref, raw_line)


def parse_dockerfile(path: Path, text: str) -> list[DependencyRef]:
    refs: list[DependencyRef] = []
    args: dict[str, str] = {}
    arg_lines: dict[str, int] = {}
    stages: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        arg = _ARG_RE.match(raw)
        if arg:
            if arg.group(2) is not None:
                args[arg.group(1)] = arg.group(2).strip("'\"")
                arg_lines[arg.group(1)] = lineno
            continue
        m = _FROM_RE.match(raw)
        if not m:
            continue
        image_text, stage = m.group(1), m.group(2)
        used = {v for groups in _VAR_RE.findall(image_text) for v in (groups[0], groups[2]) if v}
        image = substitute(image_text, args)
        refers_to_stage = image.lower() == "scratch" or image.lower() in stages
        if stage:
            stages.add(stage.lower())
        if refers_to_stage:
            continue
        note = ", ".join(
            f"set via ARG {name} on line {arg_lines[name]}"
            for name in sorted(used)
            if name in arg_lines
        )
        ref = image_ref(path, lineno, image, raw_line=raw, section="FROM", note=note)
        if ref is not None:
            refs.append(ref)
    return refs


def parse_compose(path: Path, text: str) -> list[DependencyRef]:
    refs: list[DependencyRef] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if raw.lstrip().startswith("#"):
            continue
        m = IMAGE_LINE_RE.match(raw)
        if not m:
            continue
        image = substitute(m.group(2), {})
        ref = image_ref(path, lineno, image, raw_line=raw, section="image")
        if ref is not None:
            refs.append(ref)
    return refs
