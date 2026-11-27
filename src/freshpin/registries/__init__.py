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


# How long a live "this version does not exist" answer is trusted. Short, because the
# version may be published any minute; long enough that an agent editing the same file
# several times before fixing a made-up pin does not re-download the package each time.
MISSING_TTL_SECONDS = 10 * 60


def _release_key(ecosystem: str, name: str, version: str) -> str:
    return f"release:{ecosystem}:{name}@{version}"


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

    def _resolve_one(
        self, ecosystem: str, name: str, query: str | None = None, *, refresh: bool = False
    ) -> Resolution:
        """``refresh`` skips a fresh cache entry (it is still the fallback when offline)."""
        query = query or f"{ecosystem}:{name}"
        try:
            registry = self.registries[canonical_ecosystem(ecosystem)]
        except (UnknownEcosystemError, KeyError) as exc:
            return Resolution(ecosystem=ecosystem, name=name, query=query, error=str(exc))
        eco = registry.ecosystem
        name = registry.normalize(name)
        key = self._key(eco, name)
        entry = self.cache.get(key)
        if entry is not None and entry.fresh and not refresh:
            return self._from_data(eco, name, query, entry.data, cached=True)
        if self.offline:
            if entry is not None:
                stale = not entry.fresh
                return self._from_data(eco, name, query, entry.data, cached=True, stale=stale)
            return Resolution(eco, name, query, error="offline and not in cache")
        try:
            data = registry.fetch(name, self.http, include_pre=self.include_pre)
        except NotFoundError:
            return Resolution(eco, name, query, error=f"not found on {eco}")
        except RegistryError as exc:
            if entry is not None:  # graceful offline fallback
                stale = not entry.fresh
                return self._from_data(eco, name, query, entry.data, cached=True, stale=stale)
            return Resolution(eco, name, query, error=str(exc))
        except Exception as exc:  # a registry bug must not take down an audit
            return Resolution(eco, name, query, error=f"{type(exc).__name__}: {exc}")
        self.cache.set(key, data)
        return self._from_data(eco, name, query, data)

    def resolve_queries(
        self, queries: Sequence[Query], *, refresh: bool = False
    ) -> list[Resolution]:
        """Resolve many queries concurrently, preserving input order."""
        unique: dict[tuple[str, str], Query] = {}
        for q in queries:
            unique.setdefault((q.ecosystem, q.name), q)
        results: dict[tuple[str, str], Resolution] = {}
        if unique:
            workers = min(self.max_workers, len(unique))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="freshpin") as pool:
                futures = {
                    key: pool.submit(
                        self._resolve_one, q.ecosystem, q.name, q.text, refresh=refresh
                    )
                    for key, q in unique.items()
                }
                results = {key: fut.result() for key, fut in futures.items()}
            self.cache.flush()
        out: list[Resolution] = []
        for q in queries:
            res = results[(q.ecosystem, q.name)]
            out.append(res if res.query == q.text else _with_query(res, q.text))
        return out

    def resolve_many(
        self, pairs: Iterable[tuple[str, str]], *, refresh: bool = False
    ) -> dict[tuple[str, str], Resolution]:
        """Resolve ``(ecosystem, name)`` pairs; returns a mapping keyed by the input pair.

        ``refresh`` fetches again even when the cache is fresh (used to confirm a pin that
        is missing from a cached version list before reporting it)."""
        items = list(dict.fromkeys(pairs))
        queries = [Query(eco, name, f"{eco}:{name}") for eco, name in items]
        resolved = self.resolve_queries(queries, refresh=refresh)  # same order as ``items``
        return dict(zip(items, resolved))

    def check_releases(
        self, items: Iterable[tuple[str, str, str]]
    ) -> dict[tuple[str, str, str], dict[str, Any]]:
        """Facts about exact ``(ecosystem, name, version)`` releases from registries that need
        a lookup per version (:meth:`Registry.fetch_release`, i.e. npm), concurrently.

        ``{"published": False}`` comes only from a live "not found", and is trusted for
        :data:`MISSING_TTL_SECONDS` only, since the version may be published any minute.
        Unknown answers (offline and not cached, network errors, registries without
        per-version lookups) are left out.
        """
        todo = list(dict.fromkeys(items))
        if not todo:
            return {}
        workers = min(self.max_workers, len(todo))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="freshpin") as pool:
            answers = list(pool.map(lambda item: self._check_release(*item), todo))
        self.cache.flush()
        return {item: facts for item, facts in zip(todo, answers) if facts is not None}

    def _check_release(self, ecosystem: str, name: str, version: str) -> dict[str, Any] | None:
        registry = self.registries.get(ecosystem)
        if registry is None:
            return None
        key = _release_key(ecosystem, name, version)
        entry = self.cache.get(key)
        if entry is not None:
            if entry.data.get("published") is False:
                if entry.age < MISSING_TTL_SECONDS:
                    return entry.data
                entry = None  # an old "missing" answer proves nothing
            elif entry.fresh or self.offline:
                return entry.data
        if self.offline:
            return None
        try:
            facts = registry.fetch_release(name, version, self.http)
        except NotFoundError:
            facts = {"published": False}
        except RegistryError:
            return entry.data if entry is not None else None
        except Exception:  # a registry bug must not take down an audit
            return None
        if facts is not None:
            self.cache.set(key, facts)
        return facts

    def confirmed_missing(self, ecosystem: str, name: str, version: str) -> bool:
        """True when a live lookup found ``version`` missing within :data:`MISSING_TTL_SECONDS`."""
        entry = self.cache.get(_release_key(ecosystem, name, version))
        return (
            entry is not None
            and entry.data.get("published") is False
            and entry.age < MISSING_TTL_SECONDS
        )

    def remember_missing(self, ecosystem: str, name: str, version: str) -> None:
        """Record that a live lookup just found ``version`` missing (:meth:`confirmed_missing`)."""
        self.cache.set(_release_key(ecosystem, name, version), {"published": False})


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
