"""Exact pins are checked against the registry: non-existent, yanked and deprecated versions."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeHttp, pypi, world
from freshpin.auditors import AuditReport, Finding, audit_paths
from freshpin.auditors.base import DependencyRef, evaluate, pinned_version, release_state
from freshpin.cache import Cache
from freshpin.cli import main
from freshpin.registries import MISSING_TTL_SECONDS, Resolver
from freshpin.registries.base import HttpError, NotFoundError, RegistryError, Resolution
from freshpin.registries.crates import CratesRegistry
from freshpin.registries.npm import NpmRegistry
from freshpin.registries.pypi import PyPIRegistry
from freshpin.report import format_audit_agent
from freshpin.versioning import parse_version
from test_hook import call, write_payload

TODAY = date(2026, 11, 26)
PYPI_FASTAPI = "https://pypi.org/pypi/fastapi/json"


def audit(path: Path, http: FakeHttp | None = None, **kwargs: Any) -> AuditReport:
    resolver = Resolver(http=http or FakeHttp(), cache=Cache(enabled=False))
    return audit_paths([path], resolver, today=TODAY, base=path.parent, **kwargs)


def by_line(report: AuditReport) -> dict[int, Finding]:
    return {f.ref.line: f for f in report.findings}


def v(text: str) -> Any:
    parsed = parse_version(text)
    assert parsed is not None
    return parsed


# --- registries: the data comes from responses freshpin already downloads --------------------


def test_pypi_lists_every_release_and_the_yanked_ones(fake_http: FakeHttp) -> None:
    details = PyPIRegistry().fetch("pydantic", fake_http)["details"]
    assert details["versions"] == ["1.10.13", "2.11.4", "2.12.0"]
    assert details["yanked"] == {"2.12.0": "broken wheel metadata"}
    assert fake_http.calls == ["https://pypi.org/pypi/pydantic/json"]  # one request, as before
    assert "yanked" not in PyPIRegistry().fetch("requests", fake_http)["details"]


def test_pypi_release_index_edge_cases() -> None:
    files = [
        {"filename": "x-2.0.tar.gz", "yanked": True, "yanked_reason": "bad <b>build</b>"},
        {"filename": "x-2.0-py3-none-any.whl", "yanked": False},
    ]
    http = FakeHttp(
        {
            "https://pypi.org/pypi/x/json": {
                "info": {"version": "2.0"},
                "releases": {
                    "0.9": [],  # registered without files: not installable
                    "1.0": [{"filename": "x-1.0.tar.gz", "yanked": True, "yanked_reason": None}],
                    "2.0": files,  # one file yanked, one not: still installable
                },
            }
        }
    )
    details = PyPIRegistry().fetch("x", http)["details"]
    assert details["versions"] == ["1.0", "2.0"]
    assert details["yanked"] == {"1.0": ""}
    info_only = FakeHttp({"https://pypi.org/pypi/y/json": {"info": {"version": "3.1.0"}}})
    assert "versions" not in PyPIRegistry().fetch("y", info_only)["details"]


def test_crates_lists_releases_only_when_the_list_is_complete(fake_http: FakeHttp) -> None:
    details = CratesRegistry().fetch("tokio", fake_http)["details"]
    assert details["versions"] == ["1.48.0", "1.47.0", "1.28.0"]
    assert details["yanked"] == {"1.47.0": "soundness bug"}
    assert "versions" not in CratesRegistry().fetch("serde", fake_http)["details"]
    partial = FakeHttp(
        {
            "https://crates.io/api/v1/crates/x": {
                "crate": {"name": "x", "max_stable_version": "1.1.0", "num_versions": 5},
                "versions": [{"num": "1.1.0"}, {"num": "1.0.0", "yanked": True}],
            }
        }
    )
    details = CratesRegistry().fetch("x", partial)["details"]
    assert "versions" not in details  # 2 of 5 listed: absence proves nothing
    assert details["yanked"] == {"1.0.0": ""}  # but a listed yank is still a yank


def test_npm_fetch_release(fake_http: FakeHttp) -> None:
    reg = NpmRegistry()
    assert reg.fetch_release("next", "14.2.3", fake_http) == {"published": True}
    assert reg.fetch_release("lodash", "4.17.20", fake_http) == {
        "published": True,
        "deprecated": "critical bug fixed in 4.17.21",
    }
    with pytest.raises(NotFoundError):
        reg.fetch_release("next", "99.0.0", fake_http)
    scoped = FakeHttp({"https://registry.npmjs.org/@types%2Fnode/20.11.0": {"version": "20.11.0"}})
    assert reg.fetch_release("@types/node", "20.11.0", scoped) == {"published": True}
    odd = FakeHttp({"https://registry.npmjs.org/x/1.0.0": {"version": "2.0.0"}})
    with pytest.raises(RegistryError, match="another version"):
        reg.fetch_release("x", "1.0.0", odd)
    with pytest.raises(RegistryError, match="invalid npm package name"):
        reg.fetch_release("../x", "1.0.0", fake_http)
    assert PyPIRegistry().fetch_release("requests", "2.28.0", fake_http) is None


def test_version_lists_stay_out_of_resolve_output(fake_http: FakeHttp, tmp_path: Path) -> None:
    res = Resolver(http=fake_http, cache=Cache(tmp_path / "c.json")).resolve("pypi", "pydantic")
    assert "versions" in res.details and "yanked" in res.details
    shown = res.to_dict()["details"]
    assert "versions" not in shown and "yanked" not in shown and shown["summary"] == "pkg"


# --- resolver: per-version lookups and live confirmation -------------------------------------


def age_entries(cache_path: Path, prefix: str, seconds: float) -> None:
    """Make the cache entries whose key starts with ``prefix`` ``seconds`` older."""
    data = json.loads(cache_path.read_text())
    for key, entry in data["entries"].items():
        if key.startswith(prefix):
            entry["ts"] -= seconds
    cache_path.write_text(json.dumps(data))


def test_check_releases_trusts_a_missing_answer_briefly(tmp_path: Path) -> None:
    http = FakeHttp()
    cache_path = tmp_path / "c.json"
    items = [("npm", "next", "14.2.3"), ("npm", "next", "99.0.0"), ("pypi", "requests", "1.0")]
    found = Resolver(http=http, cache=Cache(cache_path)).check_releases(items)
    assert found == {
        ("npm", "next", "14.2.3"): {"published": True},
        ("npm", "next", "99.0.0"): {"published": False},
    }  # PyPI needs no per-version lookup: its package lookup lists every release
    calls = len(http.calls)
    again = Resolver(http=http, cache=Cache(cache_path)).check_releases(items)
    assert again == found and http.calls[calls:] == []  # both answers are cached
    offline = Resolver(http=http, cache=Cache(cache_path), offline=True)
    assert offline.check_releases(items) == found
    # A "missing" answer is trusted for minutes only: the version may be published since.
    age_entries(cache_path, "release:npm:next@99.0.0", MISSING_TTL_SECONDS + 1)
    offline = Resolver(http=http, cache=Cache(cache_path), offline=True)
    assert offline.check_releases(items) == {("npm", "next", "14.2.3"): {"published": True}}
    assert Resolver(http=http, cache=Cache(cache_path)).check_releases(items) == found
    assert http.calls[calls:] == ["https://registry.npmjs.org/next/99.0.0"]  # asked again
    broken = FakeHttp({"https://registry.npmjs.org/next/15.0.0": HttpError("network down")})
    assert (
        Resolver(http=broken, cache=Cache(enabled=False)).check_releases(
            [("npm", "next", "15.0.0")]
        )
        == {}
    )


def test_resolve_many_refresh_skips_a_fresh_cache_entry(tmp_path: Path) -> None:
    http = FakeHttp()
    cache_path = tmp_path / "c.json"
    Resolver(http=http, cache=Cache(cache_path)).resolve_many([("pypi", "fastapi")])
    cached = Resolver(http=http, cache=Cache(cache_path)).resolve_many([("pypi", "fastapi")])
    assert cached[("pypi", "fastapi")].cached and http.calls.count(PYPI_FASTAPI) == 1
    fresh = Resolver(http=http, cache=Cache(cache_path)).resolve_many(
        [("pypi", "fastapi")], refresh=True
    )
    assert not fresh[("pypi", "fastapi")].cached and http.calls.count(PYPI_FASTAPI) == 2


# --- what counts as an exact pin, and what the data says about it ------------------------------


def _ref(kind: str, spec: str, ecosystem: str = "pypi") -> DependencyRef:
    return DependencyRef(
        file=Path("x"),
        line=1,
        ecosystem=ecosystem,
        name="x",
        spec=spec,
        kind=kind,  # type: ignore[arg-type]
        display_name="x",
        template="x{spec}",
    )


@pytest.mark.parametrize(
    ("kind", "ecosystem", "spec", "pinned"),
    [
        ("pep508", "pypi", "==2.28.0", "2.28.0"),
        ("pep508", "pypi", "===2.28.0", "2.28.0"),
        ("pep508", "pypi", "==2.*", None),
        ("pep508", "pypi", ">=2.28,<3", None),
        ("pep508", "pypi", "", None),
        ("poetry", "pypi", "1.2.3", "1.2.3"),
        ("poetry", "pypi", "^1.2.3", None),
        ("npm", "npm", "1.2.3", "1.2.3"),
        ("npm", "npm", "=1.2.3", "1.2.3"),
        ("npm", "npm", "v1.2.3", "1.2.3"),
        ("npm", "npm", "1.2.3-beta.1", "1.2.3-beta.1"),
        ("npm", "npm", "1.2", None),  # a range: >=1.2.0 <1.3.0
        ("npm", "npm", "~1.2.3", None),
        ("cargo", "crates", "=1.2.3", "1.2.3"),
        ("cargo", "crates", "1.2.3", None),  # Cargo's bare version is a caret range
        ("gha", "gha", "v4.1.0", None),
        ("cycle", "eol", "3.12", None),
        ("pep508", "pypi", "==not a version", None),
    ],
)
def test_pinned_version(kind: str, ecosystem: str, spec: str, pinned: str | None) -> None:
    found = pinned_version(_ref(kind, spec, ecosystem))
    assert (found.raw if found is not None else None) == pinned


def test_release_state() -> None:
    listed = Resolution(
        "pypi",
        "x",
        "pypi:x",
        version="2.32.0",
        details={"versions": ["2.31.0", "2.32.0", "3.0rc1"], "yanked": {"2.31.0": "oops"}},
    )
    assert release_state(listed, v("2.32")) == ("published", "")  # PEP 440 zero padding
    assert release_state(listed, v("3.0.0rc1")) == ("published", "")
    assert release_state(listed, v("2.31.0")) == ("yanked", "oops")
    assert release_state(listed, v("9.9.9")) == ("missing", "")
    cached = Resolution(
        "pypi", "x", "pypi:x", version="2.32.0", cached=True, details=listed.details
    )
    assert release_state(cached, v("9.9.9")) == ("unconfirmed", "")
    assert release_state(cached, v("2.31.0"))[0] == "yanked"  # yanks are trusted from cache

    npm = Resolution(
        "npm",
        "x",
        "npm:x",
        version="2.0.0",
        details={
            "releases": {
                "1.0.0": {"published": True, "deprecated": "use 2.x"},
                "1.1.0": {"published": True},
                "9.0.0": {"published": False},
            }
        },
    )
    assert release_state(npm, v("1.0.0")) == ("yanked", "use 2.x")
    assert release_state(npm, v("1.1.0")) == ("published", "")
    assert release_state(npm, v("9.0.0")) == ("missing", "")
    assert release_state(npm, v("2.0.0")) == ("published", "")  # the latest release itself
    assert release_state(npm, v("1.5.0")) == ("unknown", "")  # never looked up
    # When the latest release is deprecated too, the whole package is: no release to move to.
    whole = Resolution(
        "npm", "x", "npm:x", version="2.0.0", details={**npm.details, "deprecated": "gone"}
    )
    assert release_state(whole, v("1.0.0")) == ("published", "")


def test_evaluate_reports_missing_and_yanked_pins() -> None:
    res = Resolution(
        "pypi",
        "x",
        "pypi:x",
        version="2.0.0",
        details={"versions": ["1.0.0", "1.5.0", "2.0.0", "3.0.0"], "yanked": {"3.0.0": ""}},
    )
    missing = evaluate(_ref("pep508", "==1.2.0"), res)
    # the fix stays on the pin's own major line instead of jumping to 2.0.0
    assert (missing.status, missing.suggested, missing.bump) == ("missing", "x==1.5.0", "minor")
    assert missing.note == (
        "1.2.0 does not exist on PyPI; 1.5.0 is the newest 1.x release (2.0.0 is the latest)"
    )
    future = evaluate(_ref("pep508", "==9.0.0"), res)
    assert (future.status, future.suggested, future.bump) == ("missing", "x==2.0.0", None)
    yanked = evaluate(_ref("pep508", "==3.0.0"), res)
    assert (yanked.status, yanked.suggested) == ("yanked", "x==2.0.0")  # a downgrade
    assert yanked.note == "3.0.0 is yanked on PyPI"
    assert evaluate(_ref("pep508", "==1.5.0"), res, level="major").status == "outdated"
    assert evaluate(_ref("pep508", "==2.0.0"), res).status == "ok"
    assert evaluate(_ref("pep508", ">=9.0"), res).status == "ok"  # only exact pins are checked
    cached = Resolution("pypi", "x", "pypi:x", version="2.0.0", cached=True, details=res.details)
    unconfirmed = evaluate(_ref("pep508", "==2.5.0"), cached)
    assert unconfirmed.status == "ok"
    assert unconfirmed.note == "2.5.0 is not in the cached PyPI version list (unconfirmed)"


def test_missing_and_yanked_pins_move_within_their_release_line() -> None:
    def res(*versions: str, latest: str, yanked: tuple[str, ...] = ()) -> Resolution:
        details = {"versions": list(versions), "yanked": dict.fromkeys(yanked, "")}
        return Resolution("pypi", "x", "pypi:x", version=latest, details=details)

    # 0.x: a minor release is breaking, so the line is 0.110.x; pre-releases never count
    zero = res("0.110.0", "0.110.3", "0.110.4rc1", "0.115.0", latest="0.115.0")
    found = evaluate(_ref("pep508", "==0.110.9"), zero)
    assert (found.suggested, found.bump) == ("x==0.110.3", None)  # a downgrade within 0.110
    assert found.note == (
        "0.110.9 does not exist on PyPI; 0.110.3 is the newest 0.110.x release"
        " (0.115.0 is the latest)"
    )
    # yanked releases on the line are skipped too
    line = res("1.0.0", "1.4.0", "1.5.0", "2.0.0", latest="2.0.0", yanked=("1.4.0", "1.5.0"))
    found = evaluate(_ref("pep508", "==1.4.0"), line)
    assert (found.status, found.suggested) == ("yanked", "x==1.0.0")
    # nothing else on the line: the latest release, flagged as a breaking upgrade
    alone = res("1.0.0", "2.0.0", latest="2.0.0", yanked=("1.0.0",))
    found = evaluate(_ref("pep508", "==1.0.0"), alone)
    assert (found.status, found.suggested, found.bump) == ("yanked", "x==2.0.0", "major")
    assert found.note == (
        "1.0.0 is yanked on PyPI; 2.0.0 is a breaking upgrade: no other 1.x release is available"
    )
    # npm lists no versions: suggest the latest, and say how to stay on the line
    npm = Resolution(
        "npm",
        "react",
        "npm:react",
        version="19.3.1",
        details={"releases": {"18.99.0": {"published": False}, "19.0.99": {"published": False}}},
    )
    found = evaluate(_ref("npm", "18.99.0", "npm"), npm)
    assert (found.status, found.suggested) == ("missing", "x19.3.1")
    assert found.note == (
        "18.99.0 does not exist on npm; 19.3.1 is a breaking upgrade; to stay on 18.x,"
        " use the newest 18.x release (npm view react@18 version)"
    )
    same_major = evaluate(_ref("npm", "19.0.99", "npm"), npm)
    assert same_major.note == "19.0.99 does not exist on npm"  # nothing to explain


def test_requirements_missing_pin_keeps_its_major(tmp_path: Path) -> None:
    req = tmp_path / "requirements.txt"
    req.write_text("django==4.2.99\n")
    (found,) = audit(req).findings
    assert (found.status, found.suggested) == ("missing", "django==4.2")
    assert found.note == (
        "4.2.99 does not exist on PyPI; 4.2 is the newest 4.x release (5.2.8 is the latest)"
    )


# --- pins that may install from somewhere other than PyPI are not checked for existence ------

MISSING = "fastapi==0.999.1"  # not on PyPI in the fixture world (latest is 0.121.3)


@pytest.mark.parametrize(
    ("options", "checked"),
    [
        ("--index-url https://pypi.corp.example/simple", False),
        ("-i https://pypi.corp.example/simple", False),
        ("--index-url=https://pypi.corp.example/simple", False),
        ("--extra-index-url https://download.pytorch.org/whl/cpu", False),
        ("--find-links ./wheels", False),
        ("-f https://example.com/wheels/", False),
        ("--no-index", False),
        ("--index-url https://pypi.org/simple", True),  # PyPI itself
        ("--extra-index-url https://pypi.org/simple/", True),
        ("-r base.txt", True),
        ("--hash-checking-mode", True),
    ],
)
def test_requirements_with_another_index(tmp_path: Path, options: str, checked: bool) -> None:
    req = tmp_path / "requirements.txt"
    req.write_text(f"{MISSING}\n{options}  # comment\n")
    (found,) = audit(req).findings
    assert found.status == ("missing" if checked else "ok")
    assert bool(found.ref.private_source) is not checked


def _pyproject(tmp_path: Path, extra: str) -> dict[str, Finding]:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\nversion = "1.0"\n'
        f'dependencies = [\n  "{MISSING}",\n  "requests==9.9.9",\n]\n\n{extra}'
    )
    # the PEP 621 entries; a Poetry table may add its own (unpinned) entry for the same name
    return {f.ref.name: f for f in audit(pyproject).findings if f.ref.kind == "pep508"}


@pytest.mark.parametrize(
    ("extra", "fastapi", "requests"),
    [
        ("", "missing", "missing"),
        # per package: uv sources (an index, git, path or workspace member)
        ('[tool.uv.sources]\nfastapi = { index = "corp" }\n', "ok", "missing"),
        ("[tool.uv.sources]\nfastapi = [\n  { index = 'a', marker = 'x' },\n]\n", "ok", "missing"),
        ("[tool.uv.sources.fastapi]\nworkspace = true\n", "ok", "missing"),
        # an explicit index only serves the packages that name it
        (
            '[[tool.uv.index]]\nname = "corp"\nurl = "https://corp.example/simple"\n'
            "explicit = true\n",
            "missing",
            "missing",
        ),
        # any other index is searched for every package
        (
            '[[tool.uv.index]]\nname = "torch"\nurl = "https://download.pytorch.org/whl"\n',
            "ok",
            "ok",
        ),
        ('[[tool.uv.index]]\nurl = "https://pypi.org/simple"\n', "missing", "missing"),
        ('[tool.uv]\nindex-url = "https://corp.example/simple"\n', "ok", "ok"),
        ("[tool.uv]\nextra-index-url = []\n", "missing", "missing"),
        ('[tool.uv]\nfind-links = [\n  "./wheels",\n]\n', "ok", "ok"),
        ("[tool.uv]\nno-index = true\n", "ok", "ok"),
        (
            '[tool.uv]\nindex = [{ name = "a", url = "https://a.example", explicit = true }]\n',
            "missing",
            "missing",
        ),
        ('[tool.uv]\nindex = [\n  { name = "a", url = "https://a.example" },\n]\n', "ok", "ok"),
        # Poetry: a dependency's own source, and sources that are not explicit
        ('[tool.poetry.dependencies]\nfastapi = { source = "corp" }\n', "ok", "missing"),
        ('[tool.poetry.dependencies.fastapi]\nsource = "corp"\n', "ok", "missing"),
        ('[tool.poetry.dependencies]\nfastapi = { source = "pypi" }\n', "missing", "missing"),
        (
            '[[tool.poetry.source]]\nname = "corp"\nurl = "https://corp.example"\n'
            'priority = "explicit"\n',
            "missing",
            "missing",
        ),
        ('[[tool.poetry.source]]\nname = "corp"\nurl = "https://corp.example"\n', "ok", "ok"),
        ('[[tool.poetry.source]]\nname = "PyPI"\npriority = "primary"\n', "missing", "missing"),
    ],
)
def test_pyproject_private_sources(tmp_path: Path, extra: str, fastapi: str, requests: str) -> None:
    found = _pyproject(tmp_path, extra)
    assert (found["fastapi"].status, found["requests"].status) == (fastapi, requests)


def test_poetry_dependency_from_a_private_source(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.poetry.dependencies]\n"
        'fastapi = { version = "0.999.0", source = "corp" }\n'
        'requests = { version = "9.9.9" }\n'
    )
    found = by_line(audit(pyproject))
    assert found[2].status == "ok" and found[2].ref.private_source == "Poetry source 'corp'"
    assert found[3].status == "missing"


# --- audits ------------------------------------------------------------------------------------


def test_requirements_missing_and_yanked_pins(tmp_path: Path) -> None:
    req = tmp_path / "requirements.txt"
    req.write_text(
        "requests==2.32.5\n"
        "fastapi==0.999.1\n"
        "pydantic==2.12.0\n"
        "django==4.2.0\n"  # 4.2 is published; 4.2.0 is the same version
        "numpy==1.26.4\n"
        "httpx>=0.99\n"  # ranges are not exact pins
    )
    found = by_line(audit(req))
    assert found[1].status == "ok"
    assert (found[2].status, found[2].suggested) == ("missing", "fastapi==0.121.3")
    assert found[2].note == "0.999.1 does not exist on PyPI"
    assert (found[3].status, found[3].suggested) == ("yanked", "pydantic==2.11.4")
    assert found[3].note == "2.12.0 is yanked on PyPI: broken wheel metadata"
    assert found[4].status == "outdated" and "does not exist" not in found[4].note
    assert found[5].status == "outdated"
    assert found[6].status == "ok"
    for kwargs in ({"level": "major"}, {"check_floors": False}):
        statuses = [f.status for f in audit(req, **kwargs).findings]
        assert statuses[1:3] == ["missing", "yanked"]  # at every level, also for libraries


def test_package_json_missing_and_deprecated_pins(tmp_path: Path) -> None:
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "dependencies": {
                    "react": "19.2.0",
                    "next": "99.0.0",
                    "lodash": "4.17.20",
                    "request": "2.88.0",
                    "typescript": "^5.9.0",
                    "vite": "=7.2.0",
                }
            },
            indent=2,
        )
    )
    http = FakeHttp()
    found = {f.ref.name: f for f in audit(pkg, http).findings}
    assert found["react"].status == "ok"
    assert (found["next"].status, found["next"].suggested) == ("missing", '"next": "16.0.1"')
    assert found["next"].note == "99.0.0 does not exist on npm"
    lodash = found["lodash"]
    assert (lodash.status, lodash.suggested) == ("yanked", '"lodash": "4.17.21"')
    assert lodash.note == "4.17.20 is deprecated on npm: critical bug fixed in 4.17.21"
    # request is deprecated as a whole: the existing note says so, but there is nothing to
    # upgrade to, so it is not reported as a withdrawn version
    assert found["request"].status == "ok"
    assert "deprecated: request has been deprecated" in found["request"].note
    assert found["typescript"].status == "ok" and found["vite"].status == "ok"
    per_version = [c for c in http.calls if not c.endswith("/latest")]
    assert sorted(per_version) == [
        "https://registry.npmjs.org/lodash/4.17.20",
        "https://registry.npmjs.org/next/99.0.0",
        "https://registry.npmjs.org/request/2.88.0",
    ]  # pins equal to the latest release and ranges cost no extra request


def test_cargo_exact_pins(tmp_path: Path) -> None:
    cargo = tmp_path / "Cargo.toml"
    cargo.write_text(
        '[package]\nname = "x"\nversion = "0.1.0"\n\n'
        '[dependencies]\ntokio = "=1.47.0"\nserde = "=1.0.999"\n\n'
        '[dev-dependencies]\ntokio = "=1.99.0"\n\n'
        '[build-dependencies]\ntokio = "1.47.0"\n'
    )
    found = by_line(audit(cargo))
    assert (found[6].status, found[6].suggested) == ("yanked", 'tokio = "=1.48.0"')
    assert found[6].note == "1.47.0 is yanked on crates.io: soundness bug"
    assert found[7].status == "ok"  # serde's response in the fixture lists no versions
    assert (found[10].status, found[10].note) == ("missing", "1.99.0 does not exist on crates.io")
    assert found[13].status == "ok"  # a caret range, not a pin of the yanked release


def test_poetry_bare_version_is_an_exact_pin(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[tool.poetry.dependencies]\nfastapi = "0.999.0"\nrequests = "^2.28"\n')
    found = by_line(audit(pyproject))
    assert (found[2].status, found[2].suggested) == ("missing", 'fastapi = "0.121.3"')
    assert found[3].status == "ok"  # a caret range is not checked


def test_missing_from_a_cached_list_is_confirmed_live(tmp_path: Path) -> None:
    """A release published after the cache was written is not reported as missing."""
    http = FakeHttp(world())
    http.routes[PYPI_FASTAPI] = pypi("0.121.3")
    cache_path = tmp_path / "cache.json"
    req = tmp_path / "requirements.txt"

    def run(line: str, *, offline: bool = False) -> Finding:
        req.write_text(line + "\n")
        resolver = Resolver(http=http, cache=Cache(cache_path, ttl=3600), offline=offline)
        return audit_paths([req], resolver, today=TODAY).findings[0]

    assert run("fastapi==0.121.3").status == "ok"  # warms the cache
    http.routes[PYPI_FASTAPI] = pypi("0.121.3", "0.123.0")  # a new release appears
    assert http.calls.count(PYPI_FASTAPI) == 1
    assert run("fastapi==0.123.0").status == "ok"
    assert http.calls.count(PYPI_FASTAPI) == 2  # the cached list lacked it: fetched again
    assert run("fastapi==0.123.0").status == "ok"
    assert http.calls.count(PYPI_FASTAPI) == 2  # the refreshed list is cached
    assert run("fastapi==0.999.0").status == "missing"  # confirmed live
    offline = run("fastapi==0.555.0", offline=True)
    assert offline.status == "ok" and "unconfirmed" in offline.note


def test_lookup_failures_never_produce_a_missing_verdict(tmp_path: Path) -> None:
    pkg = tmp_path / "package.json"
    pkg.write_text('{"dependencies": {"next": "15.0.0"}}')
    http = FakeHttp(world())
    http.routes["https://registry.npmjs.org/next/15.0.0"] = HttpError("network down")
    (finding,) = audit(pkg, http).findings
    assert finding.status == "outdated"  # 15.0.0 vs 16.0.1, and existence is unknown


def test_a_pin_confirmed_missing_is_not_fetched_again_for_minutes(tmp_path: Path) -> None:
    http = FakeHttp(world())
    cache_path = tmp_path / "cache.json"
    req = tmp_path / "requirements.txt"

    def run(line: str) -> Finding:
        req.write_text(line + "\n")
        resolver = Resolver(http=http, cache=Cache(cache_path, ttl=3600))
        return audit_paths([req], resolver, today=TODAY).findings[0]

    assert run("fastapi==0.121.3").status == "ok"  # warms the cache
    assert run("fastapi==0.999.0").status == "missing"  # not in the cached list: re-checked
    assert http.calls.count(PYPI_FASTAPI) == 2
    assert run("fastapi==0.999.0").status == "missing"  # the agent edited the file again
    assert http.calls.count(PYPI_FASTAPI) == 2  # no third download within the window
    assert run("fastapi==0.888.0").status == "missing"  # another version: checked live
    assert http.calls.count(PYPI_FASTAPI) == 3
    age_entries(cache_path, "release:pypi:fastapi@0.999.0", MISSING_TTL_SECONDS + 1)
    assert run("fastapi==0.999.0").status == "missing"
    assert http.calls.count(PYPI_FASTAPI) == 4  # the remembered answer expired


def test_unconfirmed_pins_are_listed_as_unverified(tmp_path: Path) -> None:
    http = FakeHttp(world())
    cache_path = tmp_path / "cache.json"
    req = tmp_path / "requirements.txt"
    req.write_text("fastapi==0.121.3\n")
    audit_paths([req], Resolver(http=http, cache=Cache(cache_path)), today=TODAY)
    http.routes[PYPI_FASTAPI] = HttpError("rate limited")  # the live re-check fails
    req.write_text("fastapi==0.999.0\n")
    report = audit_paths([req], Resolver(http=http, cache=Cache(cache_path)), today=TODAY)
    (finding,) = report.findings
    assert finding.status == "ok" and finding.unconfirmed and report.unconfirmed == [finding]
    assert finding.to_dict()["unconfirmed"] is True
    assert format_audit_agent(report).splitlines() == [
        "freshpin: OK - 1 checked, none outdated.",
        "unverified: pypi:fastapi ==0.999.0 may not exist (not in the cached version list,"
        " and it could not be checked live)",
    ]


# --- CLI and hook ------------------------------------------------------------------------------


@pytest.fixture
def patched(patch_http: Callable[..., FakeHttp]) -> FakeHttp:
    return patch_http()


def cli(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    return main(list(argv), out=out), out.getvalue()


def test_cli_fail_on_outdated_fails_on_a_missing_pin(patched: FakeHttp, tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests==2.32.5\nfastapi==0.999.1\n")
    code, out = cli("audit", str(tmp_path), "-f", "agent", "--fail-on-outdated")
    assert code == 1
    lines = out.splitlines()
    assert lines[0] == "freshpin: 1 to fix of 2 checked (1 missing) - use the versions shown:"
    assert lines[1].endswith(
        "requirements.txt:2 | fastapi==0.999.1 -> fastapi==0.121.3 [missing]"
        " 0.999.1 does not exist on PyPI"
    )
    assert cli("audit", str(tmp_path))[0] == 0  # without the flag, findings do not fail
    code, out = cli("audit", str(tmp_path), "--json")
    data = json.loads(out)
    assert data["summary"]["missing"] == 1 and data["summary"]["ok"] == 1
    assert data["findings"][1]["status"] == "missing"
    # one field to filter on, whatever the status (missing GitHub refs were "outdated" in 0.1)
    assert [f["flagged"] for f in data["findings"]] == [False, True]
    _, table = cli("audit", str(tmp_path))
    assert "missing" in table and "-> fastapi==0.121.3" in table
    assert "1 missing, " in table and "1 ok" in table


def test_cli_agent_header_counts_each_kind(patched: FakeHttp, tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests==2.28.0\npydantic==2.12.0\n")
    code, out = cli("audit", str(tmp_path), "-f", "agent", "--fail-on-outdated")
    assert code == 1
    lines = out.splitlines()
    assert lines[0] == (
        "freshpin: 2 to fix of 2 checked (1 outdated, 1 yanked) - use the versions shown:"
    )
    assert "requirements.txt:1 | requests==2.28.0 -> requests==2.32.5 [minor] " in lines[1]
    assert lines[2].endswith(
        "pydantic==2.12.0 -> pydantic==2.11.4 [yanked] 2.12.0 is yanked on PyPI: "
        "broken wheel metadata"
    )


def test_cli_resolve_json_hides_version_lists(patched: FakeHttp) -> None:
    code, out = cli("resolve", "pypi:pydantic", "--json")
    assert code == 0
    assert "versions" not in json.loads(out)[0]["details"]


def test_hook_blocks_on_a_missing_pin(patched: FakeHttp, tmp_path: Path) -> None:
    req = tmp_path / "requirements.txt"
    req.write_text("requests==2.32.5\nfastapi==0.999.1\n")
    code, out = call(write_payload(req))
    assert code == 0 and out is not None and out["decision"] == "block"
    assert "requirements.txt:2 | fastapi==0.999.1 -> fastapi==0.121.3 [missing]" in out["reason"]
    req.write_text("requests==2.32.5\n")
    assert call(write_payload(req)) == (0, None)


def test_missing_and_yanked_lines_carry_the_bump_size(patched: FakeHttp, tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests==2.29.0\n")  # 2.29.0 was never released
    _, out = cli("audit", str(tmp_path), "-f", "agent")
    assert out.splitlines()[1].endswith(
        "requests==2.29.0 -> requests==2.32.5 [missing, minor] 2.29.0 does not exist on PyPI"
    )
    assert "missing (minor)" in cli("audit", str(tmp_path))[1]


def test_hook_mentions_unconfirmed_pins_without_blocking(patched: FakeHttp, tmp_path: Path) -> None:
    req = tmp_path / "requirements.txt"
    req.write_text("fastapi==0.121.3\n")
    assert call(write_payload(req)) == (0, None)  # warms the cache
    patched.routes[PYPI_FASTAPI] = HttpError("rate limited")
    req.write_text("fastapi==0.999.0\n")
    code, out = call(write_payload(req))
    assert code == 0 and out is not None and "decision" not in out
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "unverified: pypi:fastapi ==0.999.0 may not exist" in context
    assert "could not confirm the versions listed as unverified" in context
