"""crates.io: ``https://crates.io/api/v1/crates/<pkg>`` (``max_stable_version``)."""

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

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _add_release_index(details: dict[str, Any], versions: Any, count: Any) -> None:
    """Record every published version and the yanked ones from the crate response.

    The list only counts as complete (so a pin missing from it is reported) when its
    length matches the crate's ``num_versions``. Yanks are recorded either way.
    """
    if not isinstance(versions, list):
        return
    nums: list[str] = []
    yanked: dict[str, str] = {}
    for item in versions:
        entry = as_dict(item)
        num = as_str(entry.get("num"))
        if num is None:
            continue
        nums.append(num)
        if entry.get("yanked") is True:
            yanked[num] = clean_text(entry.get("yank_message")) or ""
    if isinstance(count, int) and count == len(nums):
        details["versions"] = nums
    if yanked:
        details["yanked"] = yanked


class CratesRegistry(Registry):
    ecosystem: ClassVar[str] = "crates"
    aliases: ClassVar[tuple[str, ...]] = ("crates", "crate", "cargo", "crates.io")
    title: ClassVar[str] = "Rust crates (crates.io)"
    example: ClassVar[str] = "crates:tokio"

    def normalize(self, name: str) -> str:
        return name.strip()

    def fetch(self, name: str, http: HttpClient, *, include_pre: bool = False) -> dict[str, Any]:
        if not _NAME_RE.match(name):
            raise RegistryError(f"invalid crate name: {name!r}")
        data = as_dict(http.get_json(f"https://crates.io/api/v1/crates/{quote(name)}"))
        crate = as_dict(data.get("crate"))
        stable = as_str(crate.get("max_stable_version"))
        newest = as_str(crate.get("max_version")) or as_str(crate.get("newest_version"))
        version = (newest or stable) if include_pre else stable
        details: dict[str, Any] = {}
        if version is None and newest:
            version = newest  # only pre-releases are published
            details["prerelease_only"] = True
        if version is None:
            raise RegistryError("no release found")
        canonical = as_str(crate.get("name")) or name
        if newest and newest != version:
            details["newest_prerelease"] = newest
        _add_release_index(details, data.get("versions"), crate.get("num_versions"))
        return {
            "version": version,
            "url": f"https://crates.io/crates/{canonical}/{version}",
            "details": details,
        }
