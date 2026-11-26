from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from conftest import FIXTURES, FakeHttp, sha_of
from freshpin.auditors import (
    AuditReport,
    audit_paths,
    detect_kind,
    discover_files,
    parse_file,
)
from freshpin.auditors.base import (
    DependencyRef,
    Finding,
    bracket_delta,
    evaluate,
    quoted_strings,
    strip_comment,
)
from freshpin.auditors.cargo_toml import classify_table, parse_cargo_toml
from freshpin.auditors.docker import parse_compose, parse_dockerfile, split_image, substitute
from freshpin.auditors.github_workflows import parse_workflow
from freshpin.auditors.package_json import parse_package_json
from freshpin.auditors.pyproject import (
    parse_pyproject,
    parse_requirement,
    parse_requirements,
    table_name,
)
from freshpin.cache import Cache
from freshpin.registries import Resolver
from freshpin.registries.base import Resolution

TODAY = date(2026, 11, 26)
STALE = FIXTURES / "stale"


def resolver() -> Resolver:
    return Resolver(http=FakeHttp(), cache=Cache(enabled=False))


@pytest.fixture(scope="module")
def stale_report() -> AuditReport:
    return audit_paths([STALE], resolver(), today=TODAY, base=STALE)


def by_location(report: AuditReport) -> dict[str, Finding]:
    return {f.location(report.base): f for f in report.findings}


# --- detection & discovery ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("pyproject.toml", "pyproject"),
        ("requirements.txt", "requirements"),
        ("requirements-dev.txt", "requirements"),
        ("dev-requirements.in", "requirements"),
        ("requirements/base.txt", "requirements"),
        ("package.json", "package_json"),
        ("Cargo.toml", "cargo"),
        (".github/workflows/ci.yml", "workflow"),
        ("sub/.github/workflows/release.yaml", "workflow"),
        ("action.yml", "workflow"),
        ("docker-compose.yml", "compose"),
        ("docker-compose.prod.yaml", "compose"),
        ("compose.yaml", "compose"),
        ("Dockerfile", "dockerfile"),
        ("Dockerfile.dev", "dockerfile"),
        ("api.Dockerfile", "dockerfile"),
        ("Containerfile", "dockerfile"),
        ("config.yml", None),
        ("README.md", None),
        ("package-lock.json", None),
        ("notes.txt", None),
    ],
)
def test_detect_kind(path: str, kind: str | None) -> None:
    assert detect_kind(Path(path)) == kind


def test_discover_skips_vendored_dirs_and_honours_exclude(tmp_path: Path) -> None:
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "package.json").write_text("{}")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "pyproject.toml").write_text("")
    (tmp_path / "pkg.egg-info").mkdir()
    (tmp_path / "pkg.egg-info" / "requirements.txt").write_text("")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("")
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "Dockerfile").write_text("")
    (tmp_path / "package.json").write_text("{}")
    found = [p.relative_to(tmp_path).as_posix() for p in discover_files(tmp_path)]
    assert found == [".github/workflows/ci.yml", "examples/Dockerfile", "package.json"]
    excluded = discover_files(tmp_path, exclude=["examples"])
    assert [p.name for p in excluded] == ["ci.yml", "package.json"]
    assert [p.name for p in discover_files(tmp_path, exclude=["*.json"])] == [
        "ci.yml",
        "Dockerfile",
    ]


# --- text helpers ---------------------------------------------------------------------------


def test_text_helpers() -> None:
    assert strip_comment('"a#b" # comment') == '"a#b" '
    assert strip_comment("x = 'y' # z") == "x = 'y' "
    assert quoted_strings('["a>=1", {include-group = "x"}, \'b\']') == ["a>=1", "b"]
    assert bracket_delta('dependencies = ["pkg[extra]>=1",') == 1
    assert bracket_delta("]") == -1
    assert table_name(' tool . "poetry" . dependencies ') == "tool.poetry.dependencies"
    assert parse_requirement("Django[argon2] (>=4.2) ; python_version > '3.8'") == (
        "Django",
        "[argon2]",
        ">=4.2",
        False,
    )
    assert parse_requirement("pkg @ https://x/pkg.whl") == ("pkg", "", "", True)
    assert parse_requirement("!!!") is None


# --- per-file parsers -------------------------------------------------------------------------


def test_pyproject_sections_and_lines() -> None:
    refs = parse_pyproject(STALE / "pyproject.toml", (STALE / "pyproject.toml").read_text())
    summary = [(r.line, r.section, r.current, r.static_status) for r in refs]
    assert (2, "build-system.requires", "hatchling>=1.27.0", None) in summary
    assert (11, "project", 'requires-python = ">=3.9"', None) in summary
    assert (17, "dependencies", "pydantic[email]>=1.10,<2", None) in summary
    assert (22, "dependencies", "mylib", "skipped") in summary  # direct URL
    assert (23, "dependencies", "uvicorn>=0.30.0", "skipped") in summary  # freshpin: ignore
    assert (27, "optional-dependencies.test", "pytest>=8.0.0", None) in summary
    assert (32, "dependency-groups.dev", "pytest>=9.2.0", None) in summary
    assert (36, "tool.poetry.dependencies", 'python = "^3.10"', None) in summary
    assert (37, "tool.poetry.dependencies", 'django = { version = "^4.2" }', None) in summary
    # strings inside the multi-line description / classifiers / inline tables are not deps
    names = {r.display_name for r in refs}
    assert "fake" not in names and "test" not in names and "Programming" not in names


def test_requirements_parsing() -> None:
    refs = parse_requirements(STALE / "requirements.txt", (STALE / "requirements.txt").read_text())
    assert [(r.line, r.current) for r in refs] == [
        (2, "requests==2.28.0"),
        (3, "fastapi==0.121.3"),
        (8, "pydantic>=2.11"),
    ]


def test_package_json_parsing() -> None:
    path = STALE / "frontend" / "package.json"
    refs = {r.display_name: r for r in parse_package_json(path, path.read_text())}
    assert refs["react"].line == 8 and refs["react"].spec == "^18.2.0"
    assert refs["@types/node"].section == "devDependencies"
    assert refs["my-ui"].static_status == "skipped"
    assert refs["legacy"].static_status == "skipped"
    alias = refs["old-react"]
    assert (alias.name, alias.spec, alias.current) == (
        "react",
        "^17.0.2",
        '"old-react": "npm:react@^17.0.2"',
    )
    assert refs["engines.node"].kind == "runtime" and refs["engines.node"].line == 5
    assert refs["vite"].spec == "latest"


def test_package_json_dist_tags_and_errors(tmp_path: Path) -> None:
    refs = parse_package_json(tmp_path / "package.json", '{"dependencies": {"x": "next"}}')
    assert refs[0].static_status == "unpinned"
    with pytest.raises(ValueError):
        parse_package_json(tmp_path / "package.json", "[1, 2]")


def test_cargo_parsing() -> None:
    path = STALE / "Cargo.toml"
    refs = {r.display_name: r for r in parse_cargo_toml(path, path.read_text())}
    assert refs["serde"].spec == "1.0" and refs["serde"].line == 7
    assert refs["reqwest"].current == 'reqwest = "0.11"'
    assert refs["local"].static_status == "skipped" and refs["local"].note == "path dependency"
    assert refs["shared"].note == "workspace dependency"
    assert refs["rand"].section == "dev-dependencies"
    assert refs["cc"].line == 17 and refs["cc"].section == "build-dependencies"
    assert refs["clap"].section == "target.cfg(unix).dependencies"


def test_cargo_renamed_and_subtable_packages(tmp_path: Path) -> None:
    text = """
[workspace.dependencies]
anyhow = "1"

[dependencies]
http02 = { package = "http", version = "0.2" }

[dependencies.ser]
package = "serde"
version = "1"
"""
    refs = parse_cargo_toml(tmp_path / "Cargo.toml", text)
    assert [(r.display_name, r.name, r.spec, r.section) for r in refs] == [
        ("anyhow", "anyhow", "1", "workspace.dependencies"),
        ("http02", "http", "0.2", "dependencies"),
        ("ser", "serde", "1", "dependencies"),
    ]
    assert classify_table("package") == (None, None)
    assert classify_table("target.x86_64-pc-windows-msvc.dev-dependencies.winapi") == (
        "target.x86_64-pc-windows-msvc.dev-dependencies",
        "winapi",
    )


def test_workflow_parsing() -> None:
    path = STALE / ".github" / "workflows" / "ci.yml"
    refs = parse_workflow(path, path.read_text())
    summary = {(r.line, r.current): r for r in refs}
    sha = summary[(20, "actions/cache@4f3dc2ef4cbd3b7d0e0eb9a76bc8b5be2d74ee95 # v3.3.2")]
    assert sha.sha and sha.spec == "v3.3.2"
    assert summary[(21, "github/codeql-action/init@v3")].name == "github/codeql-action"
    assert summary[(8, "postgres:14")].ecosystem == "eol"
    assert summary[(23, "alpine:3.18")].name == "alpine-linux"
    assert summary[(24, "acme/branch-action@main")].static_status == "unpinned"
    assert "quote it" in summary[(16, "python-version: 3.10")].note
    assert summary[(19, 'node-version: "18.x"')].spec == "18"
    matrix = [r for r in refs if r.eol_only]
    assert [r.spec for r in matrix] == ["3.8", "3.12", "3.14"]
    assert not any(r.current.startswith("./") for r in refs)


def test_workflow_sha_without_comment_is_skipped(tmp_path: Path) -> None:
    text = "steps:\n  - uses: actions/checkout@" + "a" * 40 + "\n"
    (ref,) = parse_workflow(tmp_path / "ci.yml", text)
    assert ref.static_status == "skipped" and "SHA" in ref.note


def test_dockerfile_parsing() -> None:
    path = STALE / "Dockerfile"
    refs = parse_dockerfile(path, path.read_text())
    assert [(r.line, r.current) for r in refs] == [
        (2, "python:3.10-slim"),
        (6, "node:18-alpine"),
        (7, "debian:bullseye-slim"),
        (8, "ubuntu:22.04"),
        (9, "ghcr.io/acme/base:1.0"),
    ]
    assert refs[0].note == "set via ARG PYTHON_VERSION on line 1"
    assert refs[4].static_status == "skipped"


def test_dockerfile_digest_and_registry(tmp_path: Path) -> None:
    text = (
        "FROM --platform=linux/amd64 docker.io/library/postgres:16.4@sha256:abc AS db\n"
        "FROM redis\nFROM $UNKNOWN\n"
    )
    refs = parse_dockerfile(tmp_path / "Dockerfile", text)
    assert refs[0].current == "docker.io/library/postgres:16.4@sha256:abc"
    assert refs[0].display_name == "postgres" and "digest" in refs[0].note
    assert refs[1].static_status == "unpinned"
    assert len(refs) == 2  # unresolved variable is ignored


def test_compose_parsing() -> None:
    path = STALE / "docker-compose.yml"
    refs = parse_compose(path, path.read_text())
    assert [(r.line, r.current, r.static_status) for r in refs] == [
        (3, "postgres:14-alpine", None),
        (5, "redis:6.2", None),
        (7, "mysql:8.0", None),
        (9, "nginx:latest", "unpinned"),
        (11, "python:3.14-slim", None),
    ]


def test_docker_helpers() -> None:
    assert split_image("localhost:5000/team/app:1.2@sha256:x") == (
        "localhost:5000/team/app",
        "1.2",
        "sha256:x",
    )
    assert split_image("python") == ("python", None, None)
    assert substitute("python:${V:-3.12}-${X}", {}) == "python:3.12-${X}"
    assert substitute("node:$NODE", {"NODE": "24"}) == "node:24"


# --- evaluation -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "status", "suggested"),
    [
        # pyproject / requirements
        ("pyproject.toml:2", "ok", None),
        ("pyproject.toml:11", "outdated", 'requires-python = ">=3.11"'),
        ("pyproject.toml:16", "outdated", "fastapi>=0.121.3"),
        ("pyproject.toml:17", "outdated", "pydantic[email]>=2.11.4"),
        ("pyproject.toml:18", "ok", None),
        ("pyproject.toml:19", "outdated", "django~=5.2"),
        ("pyproject.toml:20", "outdated", "numpy==2.3.4"),
        ("pyproject.toml:21", "unpinned", None),
        ("pyproject.toml:23", "skipped", None),
        ("pyproject.toml:27", "outdated", "pytest>=9.2.0"),
        ("pyproject.toml:32", "ok", None),
        ("pyproject.toml:36", "outdated", 'python = "^3.11"'),
        ("pyproject.toml:37", "outdated", 'django = { version = "^5.2" }'),
        ("requirements.txt:2", "outdated", "requests==2.32.5"),
        ("requirements.txt:3", "ok", None),
        ("requirements.txt:8", "ok", None),
        # package.json
        ("frontend/package.json:5", "outdated", '"node": ">=22"'),
        ("frontend/package.json:8", "outdated", '"react": "^19.2.0"'),
        ("frontend/package.json:9", "ok", None),
        ("frontend/package.json:10", "outdated", '"next": "16.0.1"'),
        ("frontend/package.json:11", "ok", None),
        ("frontend/package.json:14", "outdated", '"old-react": "npm:react@^19.2.0"'),
        ("frontend/package.json:17", "ok", None),
        ("frontend/package.json:18", "outdated", '"@types/node": "^24.10.0"'),
        ("frontend/package.json:19", "unpinned", None),
        # Cargo.toml
        ("Cargo.toml:7", "ok", None),
        ("Cargo.toml:8", "ok", None),
        ("Cargo.toml:9", "outdated", 'reqwest = "0.12"'),
        ("Cargo.toml:14", "outdated", 'rand = "0.9"'),
        ("Cargo.toml:17", "ok", None),
        ("Cargo.toml:20", "outdated", 'clap = "4"'),
        # workflows
        (".github/workflows/ci.yml:8", "outdated", "postgres:18"),
        (".github/workflows/ci.yml:13", "outdated", "actions/checkout@v5"),
        (".github/workflows/ci.yml:14", "ok", None),
        (".github/workflows/ci.yml:16", "outdated", "python-version: 3.14"),
        (".github/workflows/ci.yml:17", "outdated", "actions/setup-node@v6.0.0"),
        (".github/workflows/ci.yml:19", "outdated", 'node-version: "24.x"'),
        (
            ".github/workflows/ci.yml:20",
            "outdated",
            f"actions/cache@{sha_of('v4.3.0')} # v4.3.0",
        ),
        (".github/workflows/ci.yml:21", "outdated", "github/codeql-action/init@v4"),
        (".github/workflows/ci.yml:23", "outdated", "alpine:3.22"),
        (".github/workflows/ci.yml:24", "unpinned", None),
        # Docker
        ("Dockerfile:2", "outdated", "python:3.14-slim"),
        ("Dockerfile:6", "outdated", "node:24-alpine"),
        ("Dockerfile:7", "outdated", "debian:trixie-slim"),
        ("Dockerfile:8", "outdated", "ubuntu:24.04"),
        ("Dockerfile:9", "skipped", None),
        ("docker-compose.yml:3", "outdated", "postgres:18-alpine"),
        ("docker-compose.yml:5", "outdated", "redis:8.2"),
        ("docker-compose.yml:7", "outdated", "mysql:8.4"),
        ("docker-compose.yml:9", "unpinned", None),
        ("docker-compose.yml:11", "ok", None),
    ],
)
def test_stale_fixture_findings(
    stale_report: AuditReport, location: str, status: str, suggested: str | None
) -> None:
    finding = by_location(stale_report)[location]
    assert finding.status == status, finding
    assert finding.suggested == suggested


def test_stale_fixture_notes(stale_report: AuditReport) -> None:
    found = by_location(stale_report)
    assert "3.9 is EOL" in found["pyproject.toml:11"].note
    assert "constraint excludes the latest release" in found["pyproject.toml:17"].note
    assert "floor is a breaking release behind" in found["pyproject.toml:16"].note
    assert "14 is EOL" in found["docker-compose.yml:3"].note
    assert "existing data needs a migration" in found["docker-compose.yml:3"].note
    assert "migration" not in found["Dockerfile:6"].note  # node is not a database
    assert "11 is EOL" in found["Dockerfile:7"].note
    assert "ARG PYTHON_VERSION" in found["Dockerfile:2"].note
    assert found[".github/workflows/ci.yml:13"].bump == "major"


def test_matrix_entries_only_flag_eol(stale_report: AuditReport) -> None:
    matrix = [f for f in stale_report.findings if f.ref.eol_only]
    assert [(f.ref.spec, f.status) for f in matrix] == [
        ("3.8", "outdated"),
        ("3.12", "ok"),
        ("3.14", "ok"),
    ]
    assert "drop it from the matrix" in matrix[0].note


def test_report_counts_and_json(stale_report: AuditReport) -> None:
    counts = stale_report.counts()
    assert counts["outdated"] == len(stale_report.outdated) == 33
    assert counts["error"] == 0
    data = stale_report.to_dict()
    assert data["summary"]["files"] == 7  # type: ignore[index]
    json.dumps(data)  # serialisable


def test_level_patch_reports_patch_drift(tmp_path: Path) -> None:
    req = tmp_path / "requirements.txt"
    req.write_text("fastapi==0.121.2\n")
    minor = audit_paths([req], resolver(), today=TODAY)
    patch = audit_paths([req], resolver(), today=TODAY, level="patch")
    assert minor.findings[0].status == "ok"
    assert patch.findings[0].status == "outdated" and patch.findings[0].bump == "patch"
    major = audit_paths([STALE], resolver(), today=TODAY, level="major")
    assert all(f.bump == "major" for f in major.outdated if f.ref.kind not in ("cycle", "runtime"))


def test_ignore_option_and_line_filter(tmp_path: Path) -> None:
    req = tmp_path / "requirements.txt"
    req.write_text("requests==2.28.0\nfastapi==0.100.0\n")
    report = audit_paths([req], resolver(), today=TODAY, ignore=["Requests"])
    assert [f.status for f in report.findings] == ["skipped", "outdated"]
    only_line_2 = audit_paths([req], resolver(), today=TODAY, line_filter=lambda r: r.line == 2)
    assert [f.ref.display_name for f in only_line_2.findings] == ["fastapi"]


def test_errors_are_reported_not_raised(tmp_path: Path) -> None:
    bad = tmp_path / "package.json"
    bad.write_text("{not json")
    unknown = tmp_path / "notes.md"
    unknown.write_text("x")
    report = audit_paths([bad, unknown, tmp_path / "missing"], resolver(), today=TODAY)
    assert len(report.errors) == 3
    assert any("unsupported file type" in e for e in report.errors)
    assert any("no such file" in e for e in report.errors)
    with pytest.raises(ValueError):
        parse_file(unknown)


def test_lookup_errors_become_error_findings(tmp_path: Path) -> None:
    req = tmp_path / "requirements.txt"
    req.write_text("does-not-exist>=1.0\n")
    (finding,) = audit_paths([req], resolver(), today=TODAY).findings
    assert finding.status == "error" and "not found on pypi" in finding.note


def _ref(kind: str, spec: str, **kw: object) -> DependencyRef:
    return DependencyRef(
        file=Path("x"),
        line=1,
        ecosystem=str(kw.pop("ecosystem", "pypi")),
        name="x",
        spec=spec,
        kind=kind,  # type: ignore[arg-type]
        display_name="x",
        template="x{spec}",
        **kw,  # type: ignore[arg-type]
    )


def test_evaluate_edge_cases() -> None:
    ok = Resolution("pypi", "x", "pypi:x", version="2.0.0")
    assert evaluate(_ref("pep508", ">=1.0,<=="), ok).status == "skipped"
    assert evaluate(_ref("pep508", "==1.0"), None).status == "error"
    weird = Resolution("pypi", "x", "pypi:x", version="nightly")
    assert evaluate(_ref("pep508", "==1.0"), weird).status == "error"
    stale = Resolution("pypi", "x", "pypi:x", version="2.0.0", stale=True)
    assert "stale cache" in evaluate(_ref("pep508", "==1.0"), stale).note
    deprecated = Resolution("npm", "x", "npm:x", version="2.0.0", details={"deprecated": "use y"})
    assert "deprecated: use y" in evaluate(_ref("npm", "^2.0.0"), deprecated).note
    gha_branch = Resolution("gha", "x", "gha:x", version="v2.0.0")
    assert evaluate(_ref("gha", "main"), gha_branch).status == "unpinned"
    assert evaluate(_ref("runtime", "*", syntax="semver"), ok).status == "unpinned"


# --- GitHub Actions refs must exist ---------------------------------------------------------


def test_gha_refs_that_do_not_exist_are_flagged(tmp_path: Path) -> None:
    """An agent's habitual ``@v1`` fails when the action publishes no ``v1`` tag or branch."""
    wf = tmp_path / ".github" / "workflows" / "release.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text(
        "jobs:\n  release:\n    steps:\n"
        "      - uses: pypa/gh-action-pypi-publish@v1\n"
        "      - uses: pypa/gh-action-pypi-publish@v1.12.4\n"
        "      - uses: pypa/gh-action-pypi-publish@release/v1\n"
        "      - uses: ruby/setup-ruby@v1\n"
        "      - uses: actions/checkout@v3\n"
    )
    for level in ("minor", "major"):
        found = by_location(audit_paths([wf], resolver(), today=TODAY, base=tmp_path, level=level))
        missing = found[".github/workflows/release.yml:4"]
        assert missing.status == "outdated"  # at every level: the job cannot start
        assert missing.suggested == "pypa/gh-action-pypi-publish@v1.14.3"
        assert "v1 is not a tag or branch of pypa/gh-action-pypi-publish" in missing.note
        assert found[".github/workflows/release.yml:6"].status == "unpinned"
        assert found[".github/workflows/release.yml:7"].status == "ok"  # v1 is a branch
        checkout = found[".github/workflows/release.yml:8"]
        assert checkout.suggested == "actions/checkout@v5"
    exact = found[".github/workflows/release.yml:5"]
    assert exact.status == "ok"  # a minor bump is below --level major
    minor = by_location(audit_paths([wf], resolver(), today=TODAY, base=tmp_path))
    assert (
        minor[".github/workflows/release.yml:5"].suggested == "pypa/gh-action-pypi-publish@v1.14.3"
    )


def _gha(version: str, **details: object) -> Resolution:
    return Resolution("gha", "a/b", "gha:a/b", version=version, details=dict(details))


def test_gha_suggestion_falls_back_to_a_ref_that_exists() -> None:
    # v1 exists but the repository never published a floating v2 tag
    f = evaluate(_ref("gha", "v1"), _gha("v2.0.0", floating=["v1"], refs_complete=True))
    assert (f.status, f.suggested, f.bump) == ("outdated", "xv2.0.0", "major")
    published = _gha("v5.0.1", floating=["v4", "v5", "v5.0"], refs_complete=True)
    assert evaluate(_ref("gha", "v4"), published).suggested == "xv5"
    # v4.1 is not in an incomplete ref list: not flagged, and v5.0 is not known to exist
    partial = _gha("v5.0.1", floating=["v5"], refs_complete=False)
    f = evaluate(_ref("gha", "v4.1"), partial)
    assert (f.status, f.suggested, f.note) == ("outdated", "xv5.0.1", "")
    assert evaluate(_ref("gha", "v4.1.2"), partial).suggested == "xv5.0.1"


def test_gha_existence_check_needs_a_complete_ref_list() -> None:
    # the REST fallback has no branches, so a missing v1 may still be a branch
    f = evaluate(_ref("gha", "v1"), _gha("v1.14.3", floating=[], refs_complete=False))
    assert f.status == "ok"
    # without a ref list only the release tag itself is known to exist
    unknown = evaluate(_ref("gha", "v3"), _gha("v5.0.0"))
    assert (unknown.status, unknown.suggested) == ("outdated", "xv5.0.0")
    assert evaluate(_ref("gha", "v5"), _gha("v5.0.0")).status == "ok"


def test_docker_suggestions_are_verified_and_repaired(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services:\n"
        "  search:\n    image: elasticsearch:7.17\n"
        "  cache:\n    image: memcached:1.5\n"
        "  db:\n    image: postgres:14\n"
        "  app:\n    image: ghcr.io/acme/postgres:14\n"
    )
    report = audit_paths([compose], resolver(), today=TODAY)
    found = {f.ref.line: f for f in report.findings}
    search = found[3]  # short tag 8.2 is not published -> full version
    assert search.suggested == "elasticsearch:8.2.3" and search.verified is True
    assert "elasticsearch:8.2 is not published" in search.note
    cache = found[5]  # nothing published -> flagged, suggestion kept
    assert cache.suggested == "memcached:1.6" and cache.verified is False
    assert "not found on Docker Hub" in cache.note
    assert found[7].verified is True  # postgres:18 exists
    assert found[9].status == "skipped"  # not a tracked image
    assert found[3].to_dict()["verified"] is True
    unverified = audit_paths([compose], resolver(), today=TODAY, verify_tags=False)
    assert all(f.verified is None for f in unverified.findings)
    assert unverified.findings[0].suggested == "elasticsearch:8.2"


def test_utf8_bom_files_parse(tmp_path: Path) -> None:
    pkg = tmp_path / "package.json"
    pkg.write_bytes(b'\xef\xbb\xbf{"dependencies": {"react": "^18.2.0"}}')
    report = audit_paths([pkg], resolver(), today=TODAY)
    assert report.errors == [] and report.findings[0].status == "outdated"


def test_prerelease_only_packages_are_not_errors(tmp_path: Path) -> None:
    req = tmp_path / "requirements.txt"
    req.write_text("onlypre>=1.0a1\n")
    (finding,) = audit_paths([req], resolver(), today=TODAY).findings
    assert finding.status == "ok" and "only pre-releases" in finding.note


def test_retired_os_variants_are_modernised(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM python:3.10-buster\nFROM python:3.10-focal\nFROM node:18-alpine3.14\n"
        "FROM node:18.20.1-alpine3.14\n"
    )
    found = audit_paths([dockerfile], resolver(), today=TODAY).findings
    assert [f.suggested for f in found] == [
        "python:3.14-trixie",  # newest supported Debian
        "python:3.14-noble",  # newest supported Ubuntu LTS
        "node:24-alpine3.22",  # newest Alpine
        "node:24.11-alpine3.22",  # full version not published with that suffix: shorter form
    ]
    assert all(f.verified for f in found)
    assert "buster variant is not published for 3.14; using trixie" in found[0].note


def test_runtime_pin_moves_forward_instead_of_widening(tmp_path: Path) -> None:
    py = tmp_path / "pyproject.toml"
    py.write_text('[project]\nname = "x"\nrequires-python = "==3.13.*"\n')
    (finding,) = audit_paths([py], resolver(), today=TODAY).findings
    assert finding.status == "outdated"
    assert finding.suggested == 'requires-python = "==3.14.*"'


def test_library_mode_skips_stale_floors(tmp_path: Path) -> None:
    req = tmp_path / "requirements.txt"
    req.write_text("django>=4.2\nrequests==2.28.0\n")
    strict = audit_paths([req], resolver(), today=TODAY)
    library = audit_paths([req], resolver(), today=TODAY, check_floors=False)
    assert [f.status for f in strict.findings] == ["outdated", "outdated"]
    assert [f.status for f in library.findings] == ["ok", "outdated"]
