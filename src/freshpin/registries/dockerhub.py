"""Docker Hub tag verification: make sure a suggested image tag is actually published.

endoflife.date knows that Elasticsearch 9.5 exists, but the official image only publishes
full-version tags (``elasticsearch:9.5.7``), so a naive ``elasticsearch:9.5`` suggestion
would not pull. Suggestions are checked against
``hub.docker.com/v2/namespaces/<ns>/repositories/<name>/tags/<tag>`` and fall back to the
full version when the short tag does not exist. Results are cached like registry lookups.
"""

from __future__ import annotations

from urllib.parse import quote

from freshpin.cache import Cache
from freshpin.registries.base import HttpClient, NotFoundError, RegistryError

HUB_TAG_URL = "https://hub.docker.com/v2/namespaces/{ns}/repositories/{name}/tags/{tag}"
_HUB_HOSTS = ("docker.io/", "index.docker.io/", "registry-1.docker.io/")


def hub_repository(repository: str) -> tuple[str, str] | None:
    """``postgres`` -> ``("library", "postgres")``; ``bitnami/redis`` -> ``("bitnami", "redis")``.

    Returns ``None`` for images on other registries (ghcr.io, quay.io, mcr...).
    """
    repo = repository.strip().lower()
    for host in _HUB_HOSTS:
        if repo.startswith(host):
            repo = repo[len(host) :]
            break
    else:
        first = repo.split("/", 1)[0]
        if "/" in repo and ("." in first or ":" in first or first == "localhost"):
            return None
    parts = repo.split("/")
    if len(parts) == 1:
        return "library", parts[0]
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    return None


class TagChecker:
    """Cached, never-raising "does this Docker Hub tag exist?" checks."""

    def __init__(self, http: HttpClient, cache: Cache, *, offline: bool = False) -> None:
        self.http = http
        self.cache = cache
        self.offline = offline

    def exists(self, repository: str, tag: str) -> bool | None:
        """``True``/``False``, or ``None`` when unknown (other registry, offline, error)."""
        hub = hub_repository(repository)
        if hub is None or not tag:
            return None
        ns, name = hub
        key = f"dockerhub:{ns}/{name}:{tag}"
        entry = self.cache.get(key)
        if entry is not None and (entry.fresh or self.offline):
            value = entry.data.get("exists")
            return value if isinstance(value, bool) else None
        if self.offline:
            return None
        url = HUB_TAG_URL.format(ns=quote(ns), name=quote(name), tag=quote(tag, safe=""))
        try:
            self.http.get_json(url)
            exists = True
        except NotFoundError:
            exists = False
        except RegistryError:
            return None
        self.cache.set(key, {"exists": exists})
        return exists
