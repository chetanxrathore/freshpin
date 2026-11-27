"""npm: ``https://registry.npmjs.org/<pkg>/latest`` (the ``latest`` dist-tag)."""

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
from freshpin.versioning import max_version, parse_version

_NAME_RE = re.compile(r"^(?:@[a-z0-9][\w.-]*/)?[a-z0-9][\w.-]*$", re.IGNORECASE)


class NpmRegistry(Registry):
    ecosystem: ClassVar[str] = "npm"
    aliases: ClassVar[tuple[str, ...]] = ("npm", "js", "node-package", "yarn", "pnpm")
    title: ClassVar[str] = "JavaScript/TypeScript packages (npm)"
    example: ClassVar[str] = "npm:next"

    def normalize(self, name: str) -> str:
        return name.strip()

    def fetch(self, name: str, http: HttpClient, *, include_pre: bool = False) -> dict[str, Any]:
        if not _NAME_RE.match(name):
            raise RegistryError(f"invalid npm package name: {name!r}")
        # Scoped packages need the slash encoded: @types/node -> @types%2Fnode
        url = f"https://registry.npmjs.org/{quote(name, safe='@')}/latest"
        data = as_dict(http.get_json(url))
        version = as_str(data.get("version"))
        if version is None:
            raise RegistryError("registry response had no version")
        details: dict[str, Any] = {}
        parsed = parse_version(version)
        if not include_pre and parsed is not None and parsed.is_prerelease:
            # Some projects point `latest` at a release candidate (prisma 8.0.0-rc.35). Use the
            # newest stable version any dist-tag points at instead (the tags doc is tiny,
            # unlike the multi-megabyte packument).
            tags = as_dict(
                http.get_json(
                    f"https://registry.npmjs.org/-/package/{quote(name, safe='@')}/dist-tags"
                )
            )
            stable = max_version([v for v in tags.values() if isinstance(v, str)])
            if stable is not None:
                details["latest_tag"] = version
                version = stable
                data = as_dict(
                    http.get_json(
                        f"https://registry.npmjs.org/{quote(name, safe='@')}/{quote(stable)}"
                    )
                )
            else:
                details["prerelease_only"] = True
        deprecated = clean_text(data.get("deprecated"))
        if deprecated:
            details["deprecated"] = deprecated
        engines = as_dict(data.get("engines"))
        node_engine = as_str(engines.get("node"))
        if node_engine:
            details["engines_node"] = node_engine
        return {
            "version": version,
            "url": f"https://www.npmjs.com/package/{name}/v/{version}",
            "details": details,
        }

    def fetch_release(self, name: str, version: str, http: HttpClient) -> dict[str, Any] | None:
        # The ``latest`` document says nothing about other versions, and the packument that
        # lists them all can be many megabytes, so ask for this one version (a few KB).
        if not _NAME_RE.match(name):
            raise RegistryError(f"invalid npm package name: {name!r}")
        url = f"https://registry.npmjs.org/{quote(name, safe='@')}/{quote(version)}"
        data = as_dict(http.get_json(url))
        if as_str(data.get("version")) != version:
            raise RegistryError(f"registry answered with another version for {version}")
        facts: dict[str, Any] = {"published": True}
        deprecated = clean_text(data.get("deprecated"))
        if deprecated:
            facts["deprecated"] = deprecated
        return facts
