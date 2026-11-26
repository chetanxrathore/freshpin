"""endoflife.date: runtimes, databases and OS base images.

``https://endoflife.date/api/<product>.json`` returns release *cycles* (``3.14``,
``18``, ``24.04``...) with their latest patch release, EOL date and LTS status.

Policy: for products that designate LTS releases (Node.js, Ubuntu, MySQL, Temurin...)
the recommendation is the newest *LTS* cycle; otherwise it is the newest supported cycle.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, ClassVar
from urllib.parse import quote

from freshdeps.registries.base import HttpClient, Registry, RegistryError, as_dict, as_str
from freshdeps.versioning import Version, parse_version, truncate

# Friendly names -> endoflife.date product slugs.
PRODUCT_ALIASES: dict[str, str] = {
    "postgres": "postgresql",
    "pg": "postgresql",
    "node": "nodejs",
    "node.js": "nodejs",
    "golang": "go",
    "mongo": "mongodb",
    "alpine": "alpine-linux",
    "httpd": "apache-http-server",
    "apache": "apache-http-server",
    "couchdb": "apache-couchdb",
    "kafka": "apache-kafka",
    "cassandra": "apache-cassandra",
    "sonarqube": "sonarqube-community",
    "temurin": "eclipse-temurin",
    "corretto": "amazon-corretto",
    "rocky": "rocky-linux",
    "rockylinux": "rocky-linux",
    "amazonlinux": "amazon-linux",
    "k8s": "kubernetes",
    "net": "dotnet",
    ".net": "dotnet",
}

# Docker image repository (without registry host / "library/") -> product slug.
DOCKER_IMAGES: dict[str, str] = {
    "python": "python",
    "node": "nodejs",
    "postgres": "postgresql",
    "bitnami/postgresql": "postgresql",
    "postgis/postgis": "postgresql",
    "mysql": "mysql",
    "bitnami/mysql": "mysql",
    "mariadb": "mariadb",
    "redis": "redis",
    "bitnami/redis": "redis",
    "redis/redis-stack-server": "redis",
    "valkey/valkey": "valkey",
    "mongo": "mongodb",
    "bitnami/mongodb": "mongodb",
    "golang": "go",
    "rust": "rust",
    "ruby": "ruby",
    "php": "php",
    "perl": "perl",
    "erlang": "erlang",
    "elixir": "elixir",
    "ubuntu": "ubuntu",
    "debian": "debian",
    "alpine": "alpine-linux",
    "fedora": "fedora",
    "rockylinux": "rocky-linux",
    "rockylinux/rockylinux": "rocky-linux",
    "almalinux": "almalinux",
    "amazonlinux": "amazon-linux",
    "nginx": "nginx",
    "nginxinc/nginx-unprivileged": "nginx",
    "httpd": "apache-http-server",
    "haproxy": "haproxy",
    "traefik": "traefik",
    "caddy": "caddy",
    "tomcat": "tomcat",
    "elasticsearch": "elasticsearch",
    "docker.elastic.co/elasticsearch/elasticsearch": "elasticsearch",
    "kibana": "kibana",
    "docker.elastic.co/kibana/kibana": "kibana",
    "rabbitmq": "rabbitmq",
    "memcached": "memcached",
    "neo4j": "neo4j",
    "apache/kafka": "apache-kafka",
    "cassandra": "apache-cassandra",
    "couchdb": "apache-couchdb",
    "influxdb": "influxdb",
    "clickhouse/clickhouse-server": "clickhouse",
    "grafana/grafana": "grafana",
    "grafana/grafana-oss": "grafana",
    "eclipse-temurin": "eclipse-temurin",
    "amazoncorretto": "amazon-corretto",
    "quay.io/keycloak/keycloak": "keycloak",
    "keycloak/keycloak": "keycloak",
    "jenkins/jenkins": "jenkins",
    "sonarqube": "sonarqube-community",
    "denoland/deno": "deno",
    "oven/bun": "bun",
    "mcr.microsoft.com/dotnet/sdk": "dotnet",
    "mcr.microsoft.com/dotnet/aspnet": "dotnet",
    "mcr.microsoft.com/dotnet/runtime": "dotnet",
    "hashicorp/terraform": "terraform",
}

_DOCKER_HUB_HOSTS = ("docker.io/", "index.docker.io/", "registry-1.docker.io/")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]*$")


def normalize_image_repository(repository: str) -> str:
    """``docker.io/library/postgres`` -> ``postgres``; keeps non-Hub registries."""
    repo = repository.strip().lower()
    for host in _DOCKER_HUB_HOSTS:
        if repo.startswith(host):
            repo = repo[len(host) :]
            break
    repo = repo.removeprefix("public.ecr.aws/docker/")
    return repo.removeprefix("library/")


def docker_product(repository: str) -> str | None:
    """Map a Docker image repository to an endoflife.date product, if we track it."""
    return DOCKER_IMAGES.get(normalize_image_repository(repository))


def canonical_product(name: str) -> str:
    slug = name.strip().lower()
    return PRODUCT_ALIASES.get(slug, slug)


def _parse_date(value: Any) -> date | None:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class Cycle:
    """One release cycle of a product (e.g. Python ``3.14``, PostgreSQL ``18``)."""

    cycle: str
    latest: str | None = None
    eol: str | bool | None = None
    lts: str | bool | None = None
    codename: str | None = None
    release_date: str | None = None

    @classmethod
    def from_api(cls, item: Mapping[str, Any]) -> Cycle | None:
        cycle = item.get("cycle")
        if not isinstance(cycle, (str, int, float)):
            return None
        eol = item.get("eol")
        lts = item.get("lts")
        return cls(
            cycle=str(cycle),
            latest=as_str(item.get("latest")),
            eol=eol if isinstance(eol, (str, bool)) else None,
            lts=lts if isinstance(lts, (str, bool)) else None,
            codename=as_str(item.get("codename")),
            release_date=as_str(item.get("releaseDate")),
        )

    @classmethod
    def from_dict(cls, item: Mapping[str, Any]) -> Cycle | None:
        cycle = as_str(item.get("cycle"))
        if cycle is None:
            return None
        eol = item.get("eol")
        lts = item.get("lts")
        return cls(
            cycle=cycle,
            latest=as_str(item.get("latest")),
            eol=eol if isinstance(eol, (str, bool)) else None,
            lts=lts if isinstance(lts, (str, bool)) else None,
            codename=as_str(item.get("codename")),
            release_date=as_str(item.get("release_date")),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"cycle": self.cycle, "latest": self.latest, "eol": self.eol}
        if self.lts not in (None, False):
            out["lts"] = self.lts
        if self.codename:
            out["codename"] = self.codename
        if self.release_date:
            out["release_date"] = self.release_date
        return out

    @property
    def version(self) -> Version | None:
        return parse_version(self.cycle)

    @property
    def latest_version(self) -> Version | None:
        return parse_version(self.latest) if self.latest else self.version

    @property
    def codename_slug(self) -> str | None:
        """``"Bookworm"`` -> ``bookworm``; ``"Noble Numbat"`` -> ``noble`` (Docker tag form)."""
        if not self.codename:
            return None
        return self.codename.split()[0].lower()

    def eol_date(self) -> date | None:
        return _parse_date(self.eol)

    def is_eol(self, today: date) -> bool:
        if self.eol is True:
            return True
        when = self.eol_date()
        return when is not None and when <= today

    def is_lts(self, today: date) -> bool:
        if self.lts is True:
            return True
        when = _parse_date(self.lts)
        return when is not None and when <= today

    def is_released(self, today: date) -> bool:
        when = _parse_date(self.release_date)
        return when is None or when <= today


def sort_cycles(cycles: Iterable[Cycle]) -> list[Cycle]:
    """Newest first; unparseable cycles keep their API order at the end."""
    items = list(cycles)
    parseable = [c for c in items if c.version is not None]
    others = [c for c in items if c.version is None]
    parseable.sort(key=lambda c: c.version or Version(raw="0", release=(0,)), reverse=True)
    return parseable + others


def pick_cycles(cycles: Iterable[Cycle], today: date) -> tuple[Cycle, Cycle]:
    """Return ``(recommended, newest)`` cycles according to the LTS-aware policy."""
    released = [c for c in sort_cycles(cycles) if c.is_released(today)]
    if not released:
        raise RegistryError("product has no released cycles")
    supported = [c for c in released if not c.is_eol(today)] or released
    newest = supported[0]
    if any(c.lts not in (None, False) for c in released):
        lts = [c for c in supported if c.is_lts(today)]
        if lts:
            return lts[0], newest
    return newest, newest


def match_cycle(cycles: Iterable[Cycle], version: Version) -> Cycle | None:
    """Find the cycle a (possibly partial) version belongs to.

    ``3.12.4`` -> cycle ``3.12``; ``7`` (Redis) -> the newest ``7.x`` cycle.
    """
    fallback: Cycle | None = None
    for cycle in sort_cycles(cycles):  # newest first
        cv = cycle.version
        if cv is None:
            continue
        n = min(cv.precision, version.precision)
        if truncate(cv, n) != truncate(version, n):
            continue
        if cv.precision <= version.precision:
            return cycle  # the version lives inside this cycle (3.12.4 in 3.12)
        if fallback is None:
            fallback = cycle  # a partial tag floats to the newest match (redis:7 -> 7.4)
    return fallback


def match_codename(cycles: Iterable[Cycle], word: str) -> Cycle | None:
    needle = word.strip().lower()
    for cycle in cycles:
        if cycle.codename_slug == needle:
            return cycle
    return None


def cycles_from_details(details: Mapping[str, Any]) -> list[Cycle]:
    raw = details.get("cycles")
    if not isinstance(raw, list):
        return []
    return [c for item in raw if isinstance(item, dict) and (c := Cycle.from_dict(item))]


class EndOfLifeRegistry(Registry):
    ecosystem: ClassVar[str] = "eol"
    aliases: ClassVar[tuple[str, ...]] = ("eol", "endoflife", "runtime", "db", "os")
    title: ClassVar[str] = "Runtimes, databases & OS images (endoflife.date)"
    example: ClassVar[str] = "eol:postgres"

    def __init__(self, today: date | None = None) -> None:
        self._today = today

    @property
    def today(self) -> date:
        return self._today or date.today()

    def normalize(self, name: str) -> str:
        return canonical_product(name)

    def fetch(self, name: str, http: HttpClient, *, include_pre: bool = False) -> dict[str, Any]:
        if not _SLUG_RE.match(name):
            raise RegistryError(f"invalid endoflife.date product: {name!r}")
        raw = http.get_json(f"https://endoflife.date/api/{quote(name)}.json")
        if not isinstance(raw, list):
            raise RegistryError("unexpected endoflife.date response")
        cycles = [c for item in raw if (c := Cycle.from_api(as_dict(item)))]
        recommended, newest = pick_cycles(cycles, self.today)
        details: dict[str, Any] = {
            "cycle": recommended.cycle,
            "eol": recommended.eol,
            "lts": recommended.is_lts(self.today),
            "cycles": [c.to_dict() for c in sort_cycles(cycles)[:40]],
        }
        if newest.cycle != recommended.cycle:
            details["newest"] = newest.latest or newest.cycle
            details["newest_cycle"] = newest.cycle
        if recommended.codename:
            details["codename"] = recommended.codename
        return {
            "version": recommended.latest or recommended.cycle,
            "url": f"https://endoflife.date/{name}",
            "details": details,
        }
