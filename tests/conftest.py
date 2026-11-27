"""Shared fixtures: an offline fake registry world so tests are fast and deterministic."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from freshpin.registries.base import HttpClient, NotFoundError

FIXTURES = Path(__file__).parent / "fixtures"

# Dates are far in the past / future so results never depend on the real "today".
OLD = "2020-01-01"
FUTURE = "2099-01-01"


def pypi(
    *versions: str,
    yanked: tuple[str, ...] = (),
    summary: str = "pkg",
    reason: str | None = None,
) -> dict[str, Any]:
    releases = {
        v: [
            {
                "filename": f"pkg-{v}.tar.gz",
                "yanked": v in yanked,
                "yanked_reason": reason if v in yanked else None,
            }
        ]
        for v in versions
    }
    stable = [v for v in versions if not any(t in v for t in ("a", "b", "rc", "dev"))]
    return {
        "info": {"version": stable[-1] if stable else versions[-1], "summary": summary},
        "releases": releases,
    }


def sha_of(tag: str, *, peeled: bool = False) -> str:
    """Deterministic fake commit SHA for a tag (``peeled`` = the commit behind an annotated tag)."""
    return hashlib.sha1(f"{tag}{'^' if peeled else ''}".encode()).hexdigest()  # noqa: S324


def git_refs(
    *tags: str, annotated: tuple[str, ...] = (), heads: tuple[str, ...] = ("main",)
) -> str:
    """A git smart-HTTP ref advertisement (pkt-lines) listing ``heads`` and ``tags``."""
    out = "001e# service=git-upload-pack\n0000"
    head = f"{sha_of('HEAD')} HEAD\x00multi_ack thin-pack symref=HEAD:refs/heads/main\n"
    out += f"{len(head) + 4:04x}{head}"
    for branch in heads:
        line = f"{sha_of(branch)} refs/heads/{branch}\n"
        out += f"{len(line) + 4:04x}{line}"
    for tag in tags:
        lines = [f"{sha_of(tag)} refs/tags/{tag}\n"]
        if tag in annotated:
            lines.append(f"{sha_of(tag, peeled=True)} refs/tags/{tag}^{{}}\n")
        out += "".join(f"{len(line) + 4:04x}{line}" for line in lines)
    return out + "0000"


@dataclass(frozen=True)
class Redirect:
    location: str


def github_repo(repo: str, release: str | None, tags: list[str], **kw: Any) -> dict[str, Any]:
    """Routes for one repository on both the token-free web path and the REST API."""
    web, api = f"https://github.com/{repo}", f"https://api.github.com/repos/{repo}"
    routes: dict[str, Any] = {
        f"{web}/releases/latest": Redirect(
            f"{web}/releases/tag/{release}" if release else f"{web}/releases"
        ),
        f"{web}.git/info/refs?service=git-upload-pack": git_refs(*tags, **kw),
        f"{api}/releases/latest": {"tag_name": release} if release else NotFoundError("404"),
        f"{api}/tags?per_page=100": [{"name": t, "commit": {"sha": sha_of(t)}} for t in tags],
    }
    return routes


def eol_cycle(cycle: str, latest: str, *, eol: str | bool = FUTURE, **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "cycle": cycle,
        "latest": latest,
        "eol": eol,
        "releaseDate": OLD,
        "lts": extra.pop("lts", False),
    }
    item.update(extra)
    return item


def world() -> dict[str, Any]:
    """URL -> JSON payload (or an exception instance to raise)."""
    return {
        # --- PyPI ---------------------------------------------------------------
        "https://pypi.org/pypi/fastapi/json": pypi(
            "0.100.0", "0.110.0", "0.121.2", "0.121.3", "0.122.0rc1"
        ),
        "https://pypi.org/pypi/requests/json": pypi("2.28.0", "2.31.0", "2.32.5"),
        "https://pypi.org/pypi/pydantic/json": pypi(
            "1.10.13", "2.11.4", "2.12.0", yanked=("2.12.0",), reason="broken wheel metadata"
        ),
        "https://pypi.org/pypi/django/json": pypi("4.2", "5.2.8"),
        "https://pypi.org/pypi/numpy/json": pypi("1.26.4", "2.3.4"),
        "https://pypi.org/pypi/uvicorn/json": pypi("0.30.0", "0.38.0"),
        "https://pypi.org/pypi/hatchling/json": pypi("1.27.0", "1.33.0"),
        "https://pypi.org/pypi/pytest/json": pypi("8.0.0", "9.2.0"),
        "https://pypi.org/pypi/httpx/json": pypi("0.27.0", "0.28.1"),
        "https://pypi.org/pypi/onlypre/json": pypi("1.0a1", "1.0b2"),
        # --- npm ----------------------------------------------------------------
        "https://registry.npmjs.org/react/latest": {"name": "react", "version": "19.2.0"},
        "https://registry.npmjs.org/react-dom/latest": {"name": "react-dom", "version": "19.2.0"},
        "https://registry.npmjs.org/next/latest": {"name": "next", "version": "16.0.1"},
        "https://registry.npmjs.org/typescript/latest": {"name": "typescript", "version": "5.9.3"},
        "https://registry.npmjs.org/@types%2Fnode/latest": {
            "name": "@types/node",
            "version": "24.10.0",
        },
        "https://registry.npmjs.org/lodash/latest": {"name": "lodash", "version": "4.17.21"},
        "https://registry.npmjs.org/vite/latest": {"name": "vite", "version": "7.2.0"},
        "https://registry.npmjs.org/request/latest": {
            "name": "request",
            "version": "2.88.2",
            "deprecated": "request has been deprecated",
        },
        # Per-version documents, fetched only for exact pins that are not the latest release.
        "https://registry.npmjs.org/next/14.2.3": {"name": "next", "version": "14.2.3"},
        "https://registry.npmjs.org/lodash/4.17.20": {
            "name": "lodash",
            "version": "4.17.20",
            "deprecated": "critical bug fixed in 4.17.21",
        },
        "https://registry.npmjs.org/request/2.88.0": {
            "name": "request",
            "version": "2.88.0",
            "deprecated": "request has been deprecated",
        },
        # --- crates.io ------------------------------------------------------------
        "https://crates.io/api/v1/crates/serde": {
            "crate": {"name": "serde", "max_stable_version": "1.0.228", "max_version": "1.0.228"}
        },
        "https://crates.io/api/v1/crates/tokio": {
            "crate": {
                "name": "tokio",
                "max_stable_version": "1.48.0",
                "max_version": "1.48.0",
                "num_versions": 3,
            },
            "versions": [
                {"num": "1.48.0", "yanked": False, "yank_message": None},
                {"num": "1.47.0", "yanked": True, "yank_message": "soundness bug"},
                {"num": "1.28.0", "yanked": False, "yank_message": None},
            ],
        },
        "https://crates.io/api/v1/crates/reqwest": {
            "crate": {
                "name": "reqwest",
                "max_stable_version": "0.12.24",
                "max_version": "0.13.0-rc.1",
            }
        },
        "https://crates.io/api/v1/crates/clap": {
            "crate": {"name": "clap", "max_stable_version": "4.5.51", "max_version": "4.5.51"}
        },
        "https://crates.io/api/v1/crates/rand": {
            "crate": {"name": "rand", "max_stable_version": "0.9.2", "max_version": "0.9.2"}
        },
        "https://crates.io/api/v1/crates/cc": {
            "crate": {"name": "cc", "max_stable_version": "1.2.41", "max_version": "1.2.41"}
        },
        "https://crates.io/api/v1/crates/onlypre": {
            "crate": {"name": "onlypre", "max_stable_version": None, "max_version": "0.1.0-alpha"}
        },
        # --- GitHub (web redirect + git refs, and the REST API) -------------------------
        # Like the real repositories, these publish floating major tags (v5 -> v5.x.y).
        **github_repo(
            "actions/checkout",
            "v5.0.0",
            ["v1", "v2", "v3", "v3.6.0", "v4", "v4.2.2", "v5", "v5.0.0"],
            annotated=("v5.0.0",),
        ),
        **github_repo("actions/setup-python", "v6.0.0", ["v5", "v5.6.0", "v6", "v6.0.0"]),
        **github_repo("actions/setup-node", "v6.0.0", ["v4", "v4.0.2", "v5", "v6", "v6.0.0"]),
        **github_repo("actions/cache", "v4.3.0", ["v3", "v3.3.2", "v4", "v4.3.0"]),
        **github_repo("docker/build-push-action", "v6.18.0", ["v6", "v6.18.0"]),
        **github_repo(
            "github/codeql-action",
            "codeql-bundle-v2.23.3",
            ["v3", "v3.31.2", "v4", "v4.31.2", "codeql-bundle-v2.23.3"],
        ),
        # No floating tags at all, only full tags and release branches (like the real one).
        **github_repo(
            "pypa/gh-action-pypi-publish",
            "v1.14.3",
            ["v1.12.4", "v1.14.3"],
            heads=("unstable/v1", "release/v1", "release/v1.14"),
        ),
        # Floats its major on a branch rather than a tag (like the real ruby/setup-ruby).
        **github_repo(
            "ruby/setup-ruby", "v1.270.0", ["v1.269.0", "v1.270.0"], heads=("master", "v1")
        ),
        **github_repo("tagonly/action", None, ["v2.1.0", "v3.0.0-beta.1", "v2.10.0"]),
        # --- Docker Hub: every tag exists unless listed explicitly ----------------------
        "https://hub.docker.com/v2/*": {"name": "tag"},
        # --- endoflife.date -------------------------------------------------------------
        "https://endoflife.date/api/python.json": [
            eol_cycle("3.14", "3.14.2"),
            eol_cycle("3.13", "3.13.9"),
            eol_cycle("3.12", "3.12.12"),
            eol_cycle("3.11", "3.11.14"),
            eol_cycle("3.10", "3.10.19", eol="2026-10-31"),
            eol_cycle("3.9", "3.9.25", eol=OLD),
            eol_cycle("3.8", "3.8.20", eol=OLD),
        ],
        "https://endoflife.date/api/postgresql.json": [
            eol_cycle("18", "18.1"),
            eol_cycle("17", "17.7"),
            eol_cycle("14", "14.20", eol=OLD),
        ],
        "https://endoflife.date/api/nodejs.json": [
            eol_cycle("25", "25.1.0", lts=False),
            eol_cycle("24", "24.11.0", lts=OLD),
            eol_cycle("22", "22.21.0", lts=OLD),
            eol_cycle("18", "18.20.8", lts=OLD, eol=OLD),
        ],
        "https://endoflife.date/api/redis.json": [
            eol_cycle("8.2", "8.2.3"),
            eol_cycle("7.4", "7.4.6"),
            eol_cycle("6.2", "6.2.20", eol=OLD),
        ],
        "https://endoflife.date/api/ubuntu.json": [
            eol_cycle("25.10", "25.10", codename="Questing Quokka"),
            eol_cycle("24.04", "24.04.3", lts=True, codename="Noble Numbat"),
            eol_cycle("22.04", "22.04.5", lts=True, codename="Jammy Jellyfish"),
            eol_cycle("20.04", "20.04.6", lts=True, codename="Focal Fossa", eol=OLD),
        ],
        "https://endoflife.date/api/debian.json": [
            eol_cycle("13", "13.1", codename="Trixie"),
            eol_cycle("12", "12.12", codename="Bookworm"),
            eol_cycle("11", "11.11", codename="Bullseye", eol=OLD),
            eol_cycle("10", "10.13", codename="Buster", eol=OLD),
        ],
        "https://endoflife.date/api/alpine-linux.json": [
            eol_cycle("3.22", "3.22.2"),
            eol_cycle("3.21", "3.21.5"),
            eol_cycle("3.18", "3.18.12", eol=OLD),
        ],
        "https://endoflife.date/api/mysql.json": [
            eol_cycle("9.5", "9.5.0"),
            eol_cycle("8.4", "8.4.7", lts=True),
            eol_cycle("8.0", "8.0.44", eol="2026-04-30"),
        ],
        "https://endoflife.date/api/go.json": [
            eol_cycle("1.25", "1.25.4"),
            eol_cycle("1.24", "1.24.10"),
            eol_cycle("1.21", "1.21.13", eol=OLD),
        ],
        "https://endoflife.date/api/elasticsearch.json": [
            eol_cycle("8.2", "8.2.3"),
            eol_cycle("7.17", "7.17.9", eol=OLD),
        ],
        "https://endoflife.date/api/memcached.json": [eol_cycle("1.6", "1.6.39")],
        "https://hub.docker.com/v2/namespaces/library/repositories/elasticsearch/tags/8.2": (
            NotFoundError("404")
        ),
        "https://hub.docker.com/v2/namespaces/library/repositories/memcached/tags/1.6": (
            NotFoundError("404")
        ),
        "https://hub.docker.com/v2/namespaces/library/repositories/memcached/tags/1.6.39": (
            NotFoundError("404")
        ),
        **{
            f"https://hub.docker.com/v2/namespaces/library/repositories/{image}/tags/{tag}": (
                NotFoundError("404")
            )
            for image, tag in [
                ("python", "3.14-buster"),
                ("python", "3.14.2-buster"),
                ("python", "3.14-focal"),
                ("python", "3.14.2-focal"),
                ("node", "24-alpine3.14"),
                ("node", "24.11.0-alpine3.14"),
                ("node", "24.11.0-alpine3.22"),
            ]
        },
        "https://endoflife.date/api/future.json": [
            {"cycle": "2", "latest": "2.0", "releaseDate": FUTURE, "eol": False},
            eol_cycle("1", "1.9"),
        ],
    }


class FakeHttp(HttpClient):
    """An :class:`HttpClient` that serves canned payloads instead of the network."""

    def __init__(self, routes: Mapping[str, Any] | None = None) -> None:
        super().__init__(retries=0)
        self.routes: dict[str, Any] = dict(world() if routes is None else routes)
        self.calls: list[str] = []
        self.headers: list[Mapping[str, str]] = []

    def _route(self, url: str, headers: Mapping[str, str] | None) -> Any:
        self.calls.append(url)
        self.headers.append(dict(headers or {}))
        if url in self.routes:
            value = self.routes[url]
        else:
            prefixes = [k for k in self.routes if k.endswith("*") and url.startswith(k[:-1])]
            if not prefixes:
                raise NotFoundError(f"not found ({url})")
            value = self.routes[max(prefixes, key=len)]
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value()
        return copy.deepcopy(value)

    def get_json(self, url: str, headers: Mapping[str, str] | None = None) -> Any:
        return self._route(url, headers)

    def get_text(self, url: str, headers: Mapping[str, str] | None = None) -> str:
        value = self._route(url, headers)
        return value if isinstance(value, str) else ""

    def get_redirect(self, url: str, headers: Mapping[str, str] | None = None) -> str | None:
        value = self._route(url, headers)
        return value.location if isinstance(value, Redirect) else None


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Never touch the real cache, token or network from tests."""
    monkeypatch.setenv("FRESHPIN_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("FRESHPIN_CACHE_TTL", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")

    def _no_network(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("tests must not use the real network")

    monkeypatch.setattr("urllib.request.urlopen", _no_network)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", _no_network)
    yield


@pytest.fixture
def fake_http() -> FakeHttp:
    return FakeHttp()


@pytest.fixture
def patch_http(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeHttp]:
    """Route every HttpClient in the process (CLI, hook) through a FakeHttp."""

    def install(routes: Mapping[str, Any] | None = None) -> FakeHttp:
        fake = FakeHttp(routes)
        for method in ("get_json", "get_text", "get_redirect"):
            monkeypatch.setattr(
                HttpClient,
                method,
                lambda self, url, headers=None, _m=method: getattr(fake, _m)(url, headers),
            )
        return fake

    return install
