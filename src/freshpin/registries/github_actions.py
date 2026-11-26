"""GitHub Actions & releases.

Two independent paths, so lookups keep working when the REST API quota (60 requests/hour
without a token) is exhausted:

* **web** (default without a token, no API quota): ``github.com/<repo>/releases/latest``
  redirects to the latest release's tag, and the public git smart-HTTP endpoint
  ``github.com/<repo>.git/info/refs`` lists every tag with its commit SHA.
* **api** (default with ``GITHUB_TOKEN``/``GH_TOKEN``): ``/repos/<repo>/releases/latest``.

Either path falls back to the other. The tag list also yields the commit SHA of the chosen
tag, so SHA-pinned workflows get an exact replacement.

The ref list also says which *floating* refs exist. Most actions publish a ``v5`` tag that
follows the latest ``v5.x.y``, but not all: ``pypa/gh-action-pypi-publish`` has only full
tags and a ``release/v1`` branch, and ``ruby/setup-ruby`` publishes ``v1`` as a branch. So
``details["uses_ref"]`` is the least precise ref that actually exists, and
``details["floating"]`` lists every ``vN``/``vN.M`` tag or branch for the auditor to check.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Collection, Iterable
from typing import Any, ClassVar
from urllib.parse import unquote

from freshpin.registries.base import (
    HttpClient,
    HttpError,
    NotFoundError,
    Registry,
    RegistryError,
    as_dict,
    as_str,
)
from freshpin.versioning import max_version, parse_version

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REF_RE = re.compile(r"([0-9a-f]{40}) refs/tags/([^\s\x00^]+)(\^\{\})?")
_HEAD_RE = re.compile(r"[0-9a-f]{40} refs/heads/([^\s\x00]+)")
_FLOATING_RE = re.compile(r"^[vV]?\d+(?:\.\d+)?$")
API = "https://api.github.com"
WEB = "https://github.com"


def github_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None


def github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def usable_ref(tag: str, available: Collection[str]) -> str:
    """The least precise ref for ``tag`` that exists in ``available``.

    ``v5.0.1`` -> ``v5`` when the repository publishes a ``v5`` tag or branch, else
    ``v5.0`` if that exists, else ``v5.0.1`` itself (a release tag always exists).
    """
    parsed = parse_version(tag)
    if parsed is None:
        return tag
    prefix = tag[:1] if tag[:1] in ("v", "V") else ""
    written = parsed.segments or tuple(str(p) for p in parsed.release)
    if tag != prefix + ".".join(written):  # "codeql-bundle-v2.23.3", "v2.0.0-rc.1"
        return tag
    for n in range(1, len(written)):
        candidate = prefix + ".".join(written[:n])
        if candidate in available:
            return candidate
    return tag


def floating_refs(names: Iterable[str]) -> list[str]:
    """The ``vN`` / ``vN.M`` refs (tags or branches) that ``uses:`` can float on."""
    found = {name for name in names if _FLOATING_RE.match(name)}
    return sorted(
        found, key=lambda name: (tuple(int(p) for p in name.lstrip("vV").split(".")), name)
    )


def parse_tag_refs(text: str) -> dict[str, str]:
    """Parse a git ref advertisement into ``{tag: commit_sha}``.

    Annotated tags appear twice (tag object, then ``^{}`` peeled commit); the peeled
    commit SHA wins because that is what ``uses: owner/repo@<sha>`` must point at.
    """
    refs: dict[str, str] = {}
    for sha, tag, peeled in _REF_RE.findall(text):
        if peeled or tag not in refs:
            refs[tag] = sha
    return refs


def parse_branch_refs(text: str) -> set[str]:
    """Branch names from a git ref advertisement (``ruby/setup-ruby`` floats ``v1`` on one)."""
    return set(_HEAD_RE.findall(text))


def _stable_tag(tag: str | None) -> str | None:
    parsed = parse_version(tag) if tag else None
    return tag if parsed is not None and not parsed.is_prerelease else None


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

    # -- the two ways to find the latest release ----------------------------------------
    @staticmethod
    def _latest_via_web(name: str, http: HttpClient) -> str | None:
        location = http.get_redirect(f"{WEB}/{name}/releases/latest")
        if location and "/releases/tag/" in location:
            return unquote(location.rsplit("/releases/tag/", 1)[1].split("?", 1)[0])
        return None  # redirected to /releases: the repository has no releases

    @staticmethod
    def _latest_via_api(name: str, http: HttpClient) -> str | None:
        try:
            release = as_dict(
                http.get_json(f"{API}/repos/{name}/releases/latest", github_headers())
            )
        except NotFoundError:
            return None  # no releases (or a private repository)
        return as_str(release.get("tag_name"))

    def _latest_release(self, name: str, http: HttpClient) -> tuple[str | None, str]:
        lookups: list[tuple[str, Callable[[str, HttpClient], str | None]]] = [
            ("web", self._latest_via_web),
            ("api", self._latest_via_api),
        ]
        if github_token():
            lookups.reverse()
        last_error: RegistryError | None = None
        for label, lookup in lookups:
            try:
                return lookup(name, http), label
            except NotFoundError:
                raise
            except RegistryError as exc:  # rate limit, network... try the other path
                last_error = exc
        raise last_error or RegistryError("no release lookup succeeded")

    def _refs(self, name: str, http: HttpClient) -> tuple[dict[str, str], set[str] | None]:
        """``({tag: sha}, branches)`` from git smart-HTTP.

        Falls back to the REST API tag list, which has no branches and only the newest
        100 tags; ``branches`` is then ``None`` to say the ref list is incomplete.
        """
        try:
            text = http.get_text(f"{WEB}/{name}.git/info/refs?service=git-upload-pack")
            refs = parse_tag_refs(text)
            if refs:
                return refs, parse_branch_refs(text)
        except NotFoundError:
            raise
        except HttpError:
            pass
        raw = http.get_json(f"{API}/repos/{name}/tags?per_page=100", github_headers())
        out: dict[str, str] = {}
        for item in raw if isinstance(raw, list) else []:
            entry = as_dict(item)
            tag = as_str(entry.get("name"))
            sha = as_str(as_dict(entry.get("commit")).get("sha"))
            if tag:
                out[tag] = sha or ""
        return out, None

    def fetch(self, name: str, http: HttpClient, *, include_pre: bool = False) -> dict[str, Any]:
        if not _REPO_RE.match(name):
            raise RegistryError(f"expected owner/repo, got {name!r}")
        tag: str | None = None
        source = "tags"
        release_error: RegistryError | None = None
        if not include_pre:
            try:
                release, via = self._latest_release(name, http)
                tag = _stable_tag(release)
                if tag:
                    source = f"release ({via})"
            except NotFoundError:
                raise
            except RegistryError as exc:
                release_error = exc
        branches: set[str] | None = None
        try:
            refs, branches = self._refs(name, http)
        except NotFoundError:
            if tag is None:
                raise
            refs = {}
        except RegistryError:
            if tag is None:
                raise release_error or RegistryError("could not list tags") from None
            refs = {}
        if tag is None:
            if not refs:
                raise RegistryError("repository has no releases or tags")
            tag = max_version(refs, include_pre=include_pre)
            if tag is None:
                raise RegistryError("no version-like tags found")
        available = set(refs) | (branches or set())
        details: dict[str, Any] = {
            "uses_ref": usable_ref(tag, available),
            "floating": floating_refs(available),
            # True only when every tag and branch was seen, so a missing ref really is missing
            "refs_complete": branches is not None,
            "source": source,
        }
        if refs.get(tag):
            details["sha"] = refs[tag]
        return {
            "version": tag,
            "url": f"{WEB}/{name}/releases/tag/{tag}",
            "details": details,
        }
