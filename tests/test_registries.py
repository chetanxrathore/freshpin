from __future__ import annotations

import io
import json
import time
import urllib.error
from datetime import date
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeHttp
from freshdeps.cache import Cache
from freshdeps.registries import (
    Query,
    Resolver,
    UnknownEcosystemError,
    canonical_ecosystem,
    get_registry,
    parse_query,
)
from freshdeps.registries.base import (
    USER_AGENT,
    HttpClient,
    HttpError,
    NotFoundError,
    RegistryError,
)
from freshdeps.registries.crates import CratesRegistry
from freshdeps.registries.endoflife import (
    Cycle,
    EndOfLifeRegistry,
    canonical_product,
    docker_product,
    match_codename,
    match_cycle,
    normalize_image_repository,
    pick_cycles,
)
from freshdeps.registries.github_actions import GitHubActionsRegistry, major_tag
from freshdeps.registries.npm import NpmRegistry
from freshdeps.registries.pypi import PyPIRegistry
from freshdeps.versioning import parse_version

TODAY = date(2026, 11, 26)


# --- HTTP client ------------------------------------------------------------------------


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _http_error(code: int, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError("https://x.test", code, "err", msg, None)


def _urlopen_sequence(monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]) -> list[Any]:
    seen: list[Any] = []

    def fake(request: Any, timeout: float) -> _Response:
        seen.append((request, timeout))
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _Response(outcome if isinstance(outcome, bytes) else json.dumps(outcome).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake)
    return seen


def test_http_success_sends_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _urlopen_sequence(monkeypatch, [{"ok": True}])
    client = HttpClient(timeout=3.0)
    assert client.get_json("https://pypi.org/pypi/x/json", {"X-Test": "1"}) == {"ok": True}
    request, timeout = seen[0]
    assert timeout == 3.0
    assert request.get_header("User-agent") == USER_AGENT
    assert USER_AGENT.startswith("freshdeps-cli/")
    assert request.get_header("X-test") == "1"


def test_http_404_raises_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    _urlopen_sequence(monkeypatch, [_http_error(404)])
    with pytest.raises(NotFoundError):
        HttpClient().get_json("https://pypi.org/pypi/nope/json")


def test_http_retries_transient_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _urlopen_sequence(
        monkeypatch, [_http_error(503), urllib.error.URLError("reset"), {"v": 1}]
    )
    assert HttpClient(retries=2, backoff=0).get_json("https://crates.io/api/v1/crates/x") == {
        "v": 1
    }
    assert len(seen) == 3


def test_http_gives_up_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    _urlopen_sequence(monkeypatch, [TimeoutError(), TimeoutError()])
    with pytest.raises(HttpError, match="network error"):
        HttpClient(retries=1, backoff=0).get_json("https://registry.npmjs.org/x/latest")


def test_http_non_retryable_status(monkeypatch: pytest.MonkeyPatch) -> None:
    _urlopen_sequence(monkeypatch, [_http_error(401)])
    with pytest.raises(HttpError) as info:
        HttpClient(retries=3, backoff=0).get_json("https://api.github.com/x")
    assert info.value.status == 401


def test_http_github_rate_limit_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _urlopen_sequence(monkeypatch, [_http_error(403, {"X-RateLimit-Remaining": "0"})])
    with pytest.raises(HttpError, match="GITHUB_TOKEN"):
        HttpClient().get_json("https://api.github.com/repos/a/b/releases/latest")


def test_http_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _urlopen_sequence(monkeypatch, [b"<html>"])
    with pytest.raises(HttpError, match="invalid JSON"):
        HttpClient().get_json("https://endoflife.date/api/x.json")


def test_http_refuses_plain_http() -> None:
    with pytest.raises(HttpError, match="non-HTTPS"):
        HttpClient().get_json("http://pypi.org/pypi/x/json")


# --- PyPI -------------------------------------------------------------------------------


def test_pypi_latest_stable_skips_prereleases_and_yanked(fake_http: FakeHttp) -> None:
    reg = PyPIRegistry()
    assert reg.fetch("fastapi", fake_http)["version"] == "0.121.3"
    assert reg.fetch("pydantic", fake_http)["version"] == "2.11.4"  # 2.12.0 is yanked
    assert reg.fetch("fastapi", fake_http, include_pre=True)["version"] == "0.122.0rc1"


def test_pypi_only_prereleases(fake_http: FakeHttp) -> None:
    with pytest.raises(RegistryError, match="no stable release"):
        PyPIRegistry().fetch("onlypre", fake_http)
    assert PyPIRegistry().fetch("onlypre", fake_http, include_pre=True)["version"] == "1.0b2"


def test_pypi_falls_back_to_info_version() -> None:
    http = FakeHttp({"https://pypi.org/pypi/x/json": {"info": {"version": "3.1.0"}}})
    assert PyPIRegistry().fetch("x", http)["version"] == "3.1.0"


def test_pypi_normalize() -> None:
    reg = PyPIRegistry()
    assert reg.normalize("Django_REST.framework") == "django-rest-framework"
    assert reg.normalize("uvicorn[standard]") == "uvicorn"


# --- npm --------------------------------------------------------------------------------


def test_npm_latest_and_scoped_packages(fake_http: FakeHttp) -> None:
    reg = NpmRegistry()
    assert reg.fetch("react", fake_http)["version"] == "19.2.0"
    assert reg.fetch("@types/node", fake_http)["version"] == "24.10.0"
    assert fake_http.calls[-1] == "https://registry.npmjs.org/@types%2Fnode/latest"


def test_npm_deprecated_and_invalid(fake_http: FakeHttp) -> None:
    data = NpmRegistry().fetch("request", fake_http)
    assert data["details"]["deprecated"].startswith("request has been deprecated")
    with pytest.raises(RegistryError, match="invalid npm package name"):
        NpmRegistry().fetch("../etc/passwd", fake_http)


def test_npm_missing_version() -> None:
    http = FakeHttp({"https://registry.npmjs.org/x/latest": {"name": "x"}})
    with pytest.raises(RegistryError):
        NpmRegistry().fetch("x", http)


# --- crates.io --------------------------------------------------------------------------


def test_crates_max_stable_version(fake_http: FakeHttp) -> None:
    reg = CratesRegistry()
    assert reg.fetch("serde", fake_http)["version"] == "1.0.228"
    data = reg.fetch("reqwest", fake_http)
    assert data["version"] == "0.12.24"
    assert data["details"]["newest_prerelease"] == "0.13.0-rc.1"
    assert reg.fetch("reqwest", fake_http, include_pre=True)["version"] == "0.13.0-rc.1"


def test_crates_errors(fake_http: FakeHttp) -> None:
    with pytest.raises(RegistryError, match="no stable"):
        CratesRegistry().fetch("onlypre", fake_http)
    with pytest.raises(RegistryError, match="invalid crate"):
        CratesRegistry().fetch("bad name", fake_http)


# --- GitHub -------------------------------------------------------------------------------


def test_gha_latest_release(fake_http: FakeHttp) -> None:
    data = GitHubActionsRegistry().fetch("actions/checkout", fake_http)
    assert data["version"] == "v5.0.0"
    assert data["details"] == {"major_tag": "v5", "source": "release"}
    assert fake_http.headers[-1]["X-GitHub-Api-Version"] == "2022-11-28"
    assert "Authorization" not in fake_http.headers[-1]


def test_gha_tag_fallbacks(fake_http: FakeHttp) -> None:
    reg = GitHubActionsRegistry()
    codeql = reg.fetch("github/codeql-action", fake_http)  # release tag is not a version
    assert codeql["version"] == "v4.31.2" and codeql["details"]["source"] == "tags"
    assert reg.fetch("tagonly/action", fake_http)["version"] == "v2.10.0"  # no releases (404)
    assert reg.fetch("tagonly/action", fake_http, include_pre=True)["version"] == "v3.0.0-beta.1"


def test_gha_uses_token(fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    GitHubActionsRegistry().fetch("actions/checkout", fake_http)
    assert fake_http.headers[-1]["Authorization"] == "Bearer test-token"


def test_gha_normalize_and_validation(fake_http: FakeHttp) -> None:
    reg = GitHubActionsRegistry()
    assert reg.normalize("github/codeql-action/init@v3") == "github/codeql-action"
    assert reg.normalize("https://github.com/Actions/Checkout.git") == "actions/checkout"
    with pytest.raises(RegistryError, match="owner/repo"):
        reg.fetch("justone", fake_http)
    empty = FakeHttp(
        {
            "https://api.github.com/repos/a/b/releases/latest": NotFoundError("x"),
            "https://api.github.com/repos/a/b/tags?per_page=100": [],
        }
    )
    with pytest.raises(RegistryError, match="no releases or tags"):
        reg.fetch("a/b", empty)


def test_major_tag() -> None:
    assert major_tag("v5.0.1") == "v5"
    assert major_tag("2.3.0") == "2"
    assert major_tag("nightly") == "nightly"


# --- endoflife.date -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("product", "version", "cycle", "lts"),
    [
        ("python", "3.14.2", "3.14", False),
        ("nodejs", "24.11.0", "24", True),  # 25 is newer but not LTS
        ("ubuntu", "24.04.3", "24.04", True),  # 25.10 is an interim release
        ("mysql", "8.4.7", "8.4", True),  # 9.x innovation releases are not LTS
        ("postgresql", "18.1", "18", False),
    ],
)
def test_eol_lts_aware_recommendation(
    fake_http: FakeHttp, product: str, version: str, cycle: str, lts: bool
) -> None:
    data = EndOfLifeRegistry(today=TODAY).fetch(product, fake_http)
    assert data["version"] == version
    assert data["details"]["cycle"] == cycle
    assert data["details"]["lts"] is lts


def test_eol_reports_newest_non_lts(fake_http: FakeHttp) -> None:
    details = EndOfLifeRegistry(today=TODAY).fetch("nodejs", fake_http)["details"]
    assert details["newest_cycle"] == "25"
    assert details["newest"] == "25.1.0"
    assert len(details["cycles"]) == 4


def test_eol_skips_unreleased_cycles(fake_http: FakeHttp) -> None:
    assert EndOfLifeRegistry(today=TODAY).fetch("future", fake_http)["version"] == "1.9"


def test_eol_bad_responses() -> None:
    reg = EndOfLifeRegistry(today=TODAY)
    with pytest.raises(RegistryError, match="unexpected"):
        reg.fetch("x", FakeHttp({"https://endoflife.date/api/x.json": {"oops": 1}}))
    with pytest.raises(RegistryError, match="no released cycles"):
        reg.fetch("x", FakeHttp({"https://endoflife.date/api/x.json": []}))
    with pytest.raises(RegistryError, match="invalid"):
        reg.fetch("../x", FakeHttp({}))


def test_eol_aliases_and_docker_mapping() -> None:
    assert canonical_product("Postgres") == "postgresql"
    assert canonical_product("alpine") == "alpine-linux"
    assert canonical_product("python") == "python"
    assert normalize_image_repository("docker.io/library/postgres") == "postgres"
    assert normalize_image_repository("public.ecr.aws/docker/library/node") == "node"
    assert docker_product("library/golang") == "go"
    assert docker_product("bitnami/redis") == "redis"
    assert docker_product("ghcr.io/acme/app") is None


def test_cycle_helpers() -> None:
    cycles = [
        Cycle("8.2", "8.2.3"),
        Cycle("7.4", "7.4.6"),
        Cycle("7.2", "7.2.11"),
        Cycle("12", "12.12", codename="Bookworm"),
    ]
    v = parse_version
    assert match_cycle(cycles, v("7.2.5") or v("0")).cycle == "7.2"  # type: ignore[union-attr]
    assert match_cycle(cycles, v("7") or v("0")).cycle == "7.4"  # type: ignore[union-attr]
    assert match_cycle(cycles, v("5") or v("0")) is None
    assert match_codename(cycles, "BOOKWORM") is cycles[3]
    assert match_codename(cycles, "sid") is None
    lts = Cycle("24", "24.1", lts="2025-10-28", eol="2028-04-30")
    assert lts.is_lts(TODAY) and not lts.is_lts(date(2025, 1, 1))
    assert not lts.is_eol(TODAY) and lts.is_eol(date(2028, 4, 30))
    assert Cycle("1", eol=True).is_eol(TODAY)
    assert Cycle("1", eol=False).latest_version == v("1")
    rec, newest = pick_cycles([Cycle("2", "2.0"), Cycle("1", "1.5", lts=True)], TODAY)
    assert (rec.cycle, newest.cycle) == ("1", "2")
    restored = Cycle.from_dict(Cycle("12", "12.1", codename="Bookworm", lts=True).to_dict())
    assert restored is not None and restored.codename_slug == "bookworm" and restored.lts is True


# --- query parsing ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "ecosystem", "name"),
    [
        ("pypi:FastAPI", "pypi", "fastapi"),
        ("pip:uvicorn[standard]>=0.30", "pypi", "uvicorn"),
        ("npm:@types/node", "npm", "@types/node"),
        ("npm:@types/node@20", "npm", "@types/node"),
        ("npm:react@18.2.0", "npm", "react"),
        ("crates:tokio", "crates", "tokio"),
        ("cargo:serde@1", "crates", "serde"),
        ("gha:actions/checkout@v4", "gha", "actions/checkout"),
        ("github:github/codeql-action/init", "gha", "github/codeql-action"),
        ("eol:postgres", "eol", "postgresql"),
        ("eol:node", "eol", "nodejs"),
        ("docker:postgres:16-alpine", "eol", "postgresql"),
        ("docker:docker.io/library/redis", "eol", "redis"),
        ("image:node", "eol", "nodejs"),
    ],
)
def test_parse_query(text: str, ecosystem: str, name: str) -> None:
    q = parse_query(text)
    assert (q.ecosystem, q.name, q.text) == (ecosystem, name, text)


def test_parse_query_default_ecosystem_and_errors() -> None:
    assert parse_query("requests", "pypi") == Query("pypi", "requests", "requests")
    with pytest.raises(UnknownEcosystemError, match="needs an ecosystem prefix"):
        parse_query("requests")
    with pytest.raises(UnknownEcosystemError, match="unknown ecosystem"):
        parse_query("maven:junit")
    with pytest.raises(UnknownEcosystemError, match="missing package"):
        parse_query("pypi:")
    with pytest.raises(UnknownEcosystemError, match=r"no endoflife\.date mapping"):
        parse_query("docker:ghcr.io/acme/app:1")
    assert canonical_ecosystem("Cargo") == "crates"
    assert get_registry("gh").ecosystem == "gha"


# --- Resolver -------------------------------------------------------------------------------


def _resolver(http: FakeHttp, tmp_path: Path, **kwargs: Any) -> Resolver:
    return Resolver(http=http, cache=Cache(tmp_path / "c.json", ttl=3600), **kwargs)


def test_resolver_uses_cache(fake_http: FakeHttp, tmp_path: Path) -> None:
    first = _resolver(fake_http, tmp_path).resolve("pypi", "requests")
    assert first.ok and first.version == "2.32.5" and not first.cached
    calls = len(fake_http.calls)
    second = _resolver(fake_http, tmp_path).resolve("pip", "Requests")
    assert second.cached and second.version == "2.32.5"
    assert len(fake_http.calls) == calls  # served from disk cache, no HTTP


def test_resolver_no_cache(fake_http: FakeHttp, tmp_path: Path) -> None:
    resolver = Resolver(http=fake_http, cache=Cache(tmp_path / "c.json", enabled=False))
    resolver.resolve("npm", "react")
    resolver.resolve("npm", "react")
    assert fake_http.calls.count("https://registry.npmjs.org/react/latest") == 2


def test_resolver_stale_fallback_when_offline(tmp_path: Path) -> None:
    cache = Cache(tmp_path / "c.json", ttl=1)
    cache.set("npm:react", {"version": "18.0.0"})
    cache.flush()
    raw = json.loads((tmp_path / "c.json").read_text())
    raw["entries"]["npm:react"]["ts"] = time.time() - 100
    (tmp_path / "c.json").write_text(json.dumps(raw))

    broken = FakeHttp({"https://registry.npmjs.org/react/latest": HttpError("network down")})
    res = Resolver(http=broken, cache=Cache(tmp_path / "c.json", ttl=1)).resolve("npm", "react")
    assert res.ok and res.stale and res.version == "18.0.0"

    offline = Resolver(http=broken, cache=Cache(tmp_path / "c.json", ttl=1), offline=True)
    assert offline.resolve("npm", "react").stale
    missing = offline.resolve("npm", "vite")
    assert not missing.ok and missing.error == "offline and not in cache"
    assert len(broken.calls) == 1


def test_resolver_errors_never_raise(fake_http: FakeHttp, tmp_path: Path) -> None:
    resolver = _resolver(fake_http, tmp_path)
    assert resolver.resolve("pypi", "does-not-exist").error == "not found on pypi"
    assert "unknown ecosystem" in (resolver.resolve("maven", "x").error or "")
    failing = FakeHttp({"https://crates.io/api/v1/crates/x": HttpError("HTTP 500 from crates.io")})
    assert _resolver(failing, tmp_path).resolve("crates", "x").error == "HTTP 500 from crates.io"
    weird = FakeHttp({"https://registry.npmjs.org/x/latest": lambda: 1 / 0})
    assert "ZeroDivisionError" in (_resolver(weird, tmp_path).resolve("npm", "x").error or "")


def test_resolver_batch_is_ordered_deduplicated_and_concurrent(
    fake_http: FakeHttp, tmp_path: Path
) -> None:
    queries = [
        parse_query(q)
        for q in [
            "pypi:fastapi",
            "npm:next",
            "crates:tokio",
            "gha:actions/checkout",
            "eol:postgres",
            "eol:python",
            "pypi:FastAPI",
        ]
    ]
    results = _resolver(fake_http, tmp_path, max_workers=4).resolve_queries(queries)
    assert [r.version for r in results] == [
        "0.121.3",
        "16.0.1",
        "1.48.0",
        "v5.0.0",
        "18.1",
        "3.14.2",
        "0.121.3",
    ]
    assert results[-1].query == "pypi:FastAPI"
    assert fake_http.calls.count("https://pypi.org/pypi/fastapi/json") == 1
    assert (tmp_path / "c.json").exists()


def test_resolve_many_and_prerelease_cache_key(fake_http: FakeHttp, tmp_path: Path) -> None:
    resolver = _resolver(fake_http, tmp_path)
    out = resolver.resolve_many([("pypi", "fastapi"), ("npm", "react")])
    assert out[("pypi", "fastapi")].version == "0.121.3"
    pre = _resolver(fake_http, tmp_path, include_pre=True).resolve("pypi", "fastapi")
    assert pre.version == "0.122.0rc1" and not pre.cached
    assert resolver.resolve_queries([]) == []
