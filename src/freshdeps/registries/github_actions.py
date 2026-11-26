"""GitHub Actions & releases: ``/repos/<owner>/<repo>/releases/latest`` with tag fallback."""

from __future__ import annotations

import os
import re
from typing import Any, ClassVar

from freshdeps.registries.base import (
    HttpClient,
    NotFoundError,
    Registry,
    RegistryError,
    as_dict,
    as_str,
)
from freshdeps.versioning import max_version, parse_version

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
API = "https://api.github.com"


def github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def major_tag(tag: str) -> str:
    """``v5.0.1`` -> ``v5``; ``2.3.0`` -> ``2`` (the floating tag most actions publish)."""
    parsed = parse_version(tag)
    if parsed is None:
        return tag
    prefix = "v" if tag[:1] in ("v", "V") else ""
    return f"{prefix}{parsed.major}"


class GitHubActionsRegistry(Registry):
    ecosystem: ClassVar[str] = "gha"
    aliases: ClassVar[tuple[str, ...]] = (
        "gha",
        "github",
        "gh",
        "action",
        "actions",
        "github-actions",
        "release",
    )
    title: ClassVar[str] = "GitHub Actions & GitHub releases"
    example: ClassVar[str] = "gha:actions/checkout"

    def normalize(self, name: str) -> str:
        # "github/codeql-action/init" -> "github/codeql-action"; strip URLs and ".git".
        text = name.strip().removeprefix("https://github.com/").removesuffix(".git")
        text = text.split("@", 1)[0]
        parts = [p for p in text.split("/") if p]
        return "/".join(parts[:2]).lower()

    def fetch(self, name: str, http: HttpClient, *, include_pre: bool = False) -> dict[str, Any]:
        if not _REPO_RE.match(name):
            raise RegistryError(f"expected owner/repo, got {name!r}")
        headers = github_headers()
        tag: str | None = None
        source = "release"
        if not include_pre:
            try:
                release = as_dict(http.get_json(f"{API}/repos/{name}/releases/latest", headers))
                candidate = as_str(release.get("tag_name"))
                parsed = parse_version(candidate) if candidate else None
                if candidate and parsed is not None and not parsed.is_prerelease:
                    tag = candidate
            except NotFoundError:
                tag = None  # no releases: fall back to tags
        if tag is None:
            source = "tags"
            raw_tags = http.get_json(f"{API}/repos/{name}/tags?per_page=100", headers)
            names = [
                n
                for item in (raw_tags if isinstance(raw_tags, list) else [])
                if (n := as_str(as_dict(item).get("name")))
            ]
            if not names:
                raise RegistryError("repository has no releases or tags")
            tag = max_version(names, include_pre=include_pre)
            if tag is None:
                raise RegistryError("no version-like tags found")
        return {
            "version": tag,
            "url": f"https://github.com/{name}/releases/tag/{tag}",
            "details": {"major_tag": major_tag(tag), "source": source},
        }
