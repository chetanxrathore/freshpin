"""crates.io: ``https://crates.io/api/v1/crates/<pkg>`` (``max_stable_version``)."""

from __future__ import annotations

import re
from typing import Any, ClassVar
from urllib.parse import quote

from freshdeps.registries.base import HttpClient, Registry, RegistryError, as_dict, as_str

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


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
        if version is None:
            raise RegistryError("no stable release found (use --pre to include pre-releases)")
        canonical = as_str(crate.get("name")) or name
        details: dict[str, Any] = {}
        if newest and newest != version:
            details["newest_prerelease"] = newest
        return {
            "version": version,
            "url": f"https://crates.io/crates/{canonical}/{version}",
            "details": details,
        }
