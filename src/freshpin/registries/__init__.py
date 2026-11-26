"""Registry lookup, ``ecosystem:package`` query parsing and concurrent resolution."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from freshpin.cache import Cache
from freshpin.registries.base import (
    HttpClient,
    NotFoundError,
    Registry,
    RegistryError,
    Resolution,
    as_dict,
    as_str,
)
from freshpin.registries.crates import CratesRegistry
from freshpin.registries.endoflife import EndOfLifeRegistry, docker_product
from freshpin.registries.github_actions import GitHubActionsRegistry
from freshpin.registries.npm import NpmRegistry
from freshpin.registries.pypi import PyPIRegistry

__all__ = [
    "REGISTRIES",
    "HttpClient",
    "Query",
    "Registry",
    "RegistryError",
    "Resolution",
    "Resolver",
    "UnknownEcosystemError",
    "canonical_ecosystem",
    "get_registry",
    "parse_query",
]

REGISTRIES: dict[str, Registry] = {
    reg.ecosystem: reg
    for reg in (
        PyPIRegistry(),
        NpmRegistry(),
        CratesRegistry(),
        GitHubActionsRegistry(),
        EndOfLifeRegistry(),
    )
}
DOCKER_ALIASES = ("docker", "image", "container", "oci")

_ALIASES: dict[str, str] = {}
for _reg in REGISTRIES.values():
    for _alias in (_reg.ecosystem, *_reg.aliases):
        _ALIASES[_alias] = _reg.ecosystem


class UnknownEcosystemError(ValueError):
    pass


def canonical_ecosystem(name: str) -> str:
    key = name.strip().lower()
    if key in _ALIASES:
        return _ALIASES[key]
    known = ", ".join(sorted(REGISTRIES)) + ", docker"
    raise UnknownEcosystemError(f"unknown ecosystem {name!r} (known: {known})")


def get_registry(ecosystem: str) -> Registry:
    return REGISTRIES[canonical_ecosystem(ecosystem)]


@dataclass(frozen=True)
class Query:
    ecosystem: str
    name: str
    text: str


def parse_query(text: str, default_ecosystem: str | None = None) -> Query:
    """Parse ``pypi:fastapi``, ``gha:actions/checkout@v4``, ``docker:postgres:16``...

    A bare name uses ``default_ecosystem``. Version suffixes are ignored: the point is
    to find the latest version, not to check a given one.
    """
    raw = text.strip()
    if ":" in raw:
        prefix, _, name = raw.partition(":")
    elif default_ecosystem:
        prefix, name = default_ecosystem, raw
    else:
        raise UnknownEcosystemError(
            f"{raw!r} needs an ecosystem prefix, e.g. pypi:{raw} (or pass --ecosystem)"
        )
    prefix = prefix.strip().lower()
    name = name.strip()
    if not name:
        raise UnknownEcosystemError(f"missing package name in {raw!r}")
    if prefix in DOCKER_ALIASES:
        image = name.split("@", 1)[0]
        repo = image.rsplit(":", 1)[0] if "/" not in image.rsplit(":", 1)[-1] else image
        product = docker_product(repo)
        if product is None:
            raise UnknownEcosystemError(
                f"no endoflife.date mapping for Docker image {repo!r}; try eol:<product>"
            )
        return Query("eol", product, raw)
    ecosystem = canonical_ecosystem(prefix)
    if ecosystem == "pypi":
        for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", ";", " "):
            name = name.split(sep, 1)[0]
    elif ecosystem in ("npm", "gha"):
        # keep the scope "@" of "@types/node" but drop "@version" suffixes
        head, sep, _ = name[1:].partition("@") if name.startswith("@") else name.partition("@")
        name = ("@" + head if name.startswith("@") else head) if sep else name
    elif ecosystem == "crates":
        name = name.split("@", 1)[0].split("=", 1)[0]
    return Query(ecosystem, REGISTRIES[ecosystem].normalize(name), raw)


class Resolver:
    """Resolve latest versions concurrently with caching and stale-cache fallback."""

    def __init__(
        self,
        *,
        http: HttpClient | None = None,
        cache: Cache | None = None,
        include_pre: bool = False,
        offline: bool = False,
        max_workers: int = 16,
        registries: Mapping[str, Registry] | None = None,
    ) -> None:
        self.http = http or HttpClient()
        self.cache = cache if cache is not None else Cache()
        self.include_pre = include_pre
        self.offline = offline
        self.max_workers = max(1, max_workers)
        self.registries = dict(registries) if registries is not None else REGISTRIES

    def _key(self, ecosystem: str, name: str) -> str:
        return f"{ecosystem}:{name}" + ("@pre" if self.include_pre else "")

    @staticmethod
    def _from_data(
        ecosystem: str,
        name: str,
        query: str,
        data: Mapping[str, Any],
        *,
        cached: bool = False,
        stale: bool = False,
    ) -> Resolution:
        return Resolution(
            ecosystem=ecosystem,
            name=name,
            query=query,
            version=as_str(data.get("version")),
            url=as_str(data.get("url")),
            cached=cached,
            stale=stale,
            details=as_dict(data.get("details")),
        )

    def resolve(self, ecosystem: str, name: str, query: str | None = None) -> Resolution:
        """Resolve one package and persist the cache. Never raises for lookup problems."""
        result = self._resolve_one(ecosystem, name, query)
        self.cache.flush()
        return result

    def _resolve_one(self, ecosystem: str, name: str, query: str | None = None) -> Resolution:
        query = query or f"{ecosystem}:{name}"
        try:
            registry = self.registries[canonical_ecosystem(ecosystem)]
        except (UnknownEcosystemError, KeyError) as exc:
            return Resolution(ecosystem=ecosystem, name=name, query=query, error=str(exc))
        eco = registry.ecosystem
        name = registry.normalize(name)
        key = self._key(eco, name)
        entry = self.cache.get(key)
        if entry is not None and entry.fresh:
            return self._from_data(eco, name, query, entry.data, cached=True)
        if self.offline:
            if entry is not None:
                return self._from_data(eco, name, query, entry.data, cached=True, stale=True)
            return Resolution(eco, name, query, error="offline and not in cache")
        try:
            data = registry.fetch(name, self.http, include_pre=self.include_pre)
        except NotFoundError:
            return Resolution(eco, name, query, error=f"not found on {eco}")
        except RegistryError as exc:
            if entry is not None:  # graceful offline fallback
                return self._from_data(eco, name, query, entry.data, cached=True, stale=True)
            return Resolution(eco, name, query, error=str(exc))
        except Exception as exc:  # a registry bug must not take down an audit
            return Resolution(eco, name, query, error=f"{type(exc).__name__}: {exc}")
        self.cache.set(key, data)
        return self._from_data(eco, name, query, data)

    def resolve_queries(self, queries: Sequence[Query]) -> list[Resolution]:
        """Resolve many queries concurrently, preserving input order."""
        unique: dict[tuple[str, str], Query] = {}
        for q in queries:
            unique.setdefault((q.ecosystem, q.name), q)
        results: dict[tuple[str, str], Resolution] = {}
        if unique:
            workers = min(self.max_workers, len(unique))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="freshpin") as pool:
                futures = {
                    key: pool.submit(self._resolve_one, q.ecosystem, q.name, q.text)
                    for key, q in unique.items()
                }
                results = {key: fut.result() for key, fut in futures.items()}
            self.cache.flush()
        out: list[Resolution] = []
        for q in queries:
            res = results[(q.ecosystem, q.name)]
            out.append(res if res.query == q.text else _with_query(res, q.text))
        return out

    def resolve_many(self, pairs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], Resolution]:
        """Resolve ``(ecosystem, name)`` pairs; returns a mapping keyed by the input pair."""
        items = list(dict.fromkeys(pairs))
        queries = [Query(eco, name, f"{eco}:{name}") for eco, name in items]
        resolved = self.resolve_queries(queries)  # same length and order as ``items``
        return dict(zip(items, resolved))


def _with_query(res: Resolution, query: str) -> Resolution:
    return Resolution(
        ecosystem=res.ecosystem,
        name=res.name,
        query=query,
        version=res.version,
        url=res.url,
        error=res.error,
        cached=res.cached,
        stale=res.stale,
        details=res.details,
    )
