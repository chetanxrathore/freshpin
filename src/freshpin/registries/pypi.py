"""PyPI: ``https://pypi.org/pypi/<pkg>/json``."""

from __future__ import annotations

import re
from typing import Any, ClassVar
from urllib.parse import quote

from freshpin.registries.base import (
    HttpClient,
    Registry,
    RegistryError,
    as_dict,
    as_str,
    clean_text,
)
from freshpin.versioning import is_stable, max_version

_NORMALIZE_RE = re.compile(r"[-_.]+")


def _has_installable_files(files: Any) -> bool:
    """A release counts if it has at least one file that is not yanked."""
    if not isinstance(files, list) or not files:
        return False
    return any(isinstance(f, dict) and not f.get("yanked", False) for f in files)


def release_index(releases: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """Every published version (one that has files) and the yanked ones with their reasons.

    PyPI yanks a release by yanking all of its files (PEP 592). pip still installs a yanked
    release when it is pinned with ``==``, which is why a yanked pin is worth flagging.
    """
    versions: list[str] = []
    yanked: dict[str, str] = {}
    for ver, files in releases.items():
        entries = [f for f in files if isinstance(f, dict)] if isinstance(files, list) else []
        if not entries:
            continue  # registered without files (or all deleted): nothing can be installed
        versions.append(ver)
        if all(f.get("yanked", False) for f in entries):
            reasons = (clean_text(f.get("yanked_reason")) for f in entries)
            yanked[ver] = next((r for r in reasons if r), "")
    return versions, yanked


class PyPIRegistry(Registry):
    ecosystem: ClassVar[str] = "pypi"
    aliases: ClassVar[tuple[str, ...]] = ("pypi", "pip", "py", "python-package")
    title: ClassVar[str] = "Python packages (PyPI)"
    example: ClassVar[str] = "pypi:fastapi"

    def normalize(self, name: str) -> str:
        # PEP 503 normalisation; also drops any extras ("uvicorn[standard]").
        base = name.strip().split("[", 1)[0]
        return _NORMALIZE_RE.sub("-", base).lower()

    def fetch(self, name: str, http: HttpClient, *, include_pre: bool = False) -> dict[str, Any]:
        url = f"https://pypi.org/pypi/{quote(name, safe='')}/json"
        data = as_dict(http.get_json(url))
        info = as_dict(data.get("info"))
        releases = as_dict(data.get("releases"))
        candidates = [ver for ver, files in releases.items() if _has_installable_files(files)]
        version = max_version(candidates, include_pre=include_pre)
        if version is None:
            # Fall back to the "info" block (PyPI's own notion of latest).
            info_version = as_str(info.get("version"))
            if info_version and (include_pre or is_stable(info_version)):
                version = info_version
        details: dict[str, Any] = {}
        if version is None:
            # Only pre-releases exist (e.g. opentelemetry-instrumentation-*): report the
            # newest one rather than failing, and say so.
            version = max_version(candidates, include_pre=True)
            if version is None:
                raise RegistryError("no installable release found")
            details["prerelease_only"] = True
        summary = clean_text(info.get("summary"))
        if summary:
            details["summary"] = summary
        requires_python = as_str(info.get("requires_python"))
        if requires_python and info.get("version") == version:
            details["requires_python"] = requires_python
        if releases:
            details["versions"], yanked = release_index(releases)
            if yanked:
                details["yanked"] = yanked
        return {
            "version": version,
            "url": f"https://pypi.org/project/{name}/{version}/",
            "details": details,
        }
