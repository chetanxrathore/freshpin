from __future__ import annotations

import io
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import FIXTURES, FakeHttp
from freshpin import __version__
from freshpin.cli import main

STALE = FIXTURES / "stale"


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), out=out)
    return code, out.getvalue()


@pytest.fixture
def world(patch_http: Callable[..., FakeHttp]) -> FakeHttp:
    return patch_http()


# --- resolve ------------------------------------------------------------------------------


def test_resolve_table(world: FakeHttp) -> None:
    code, out = run(
        "resolve",
        "pypi:fastapi",
        "npm:next",
        "crates:tokio",
        "gha:actions/checkout",
        "eol:postgres",
        "eol:python",
    )
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split() == ["QUERY", "LATEST", "NOTES"]
    assert "0.121.3" in lines[1]
    assert "uses: actions/checkout@v5" in out
    assert "docker postgres:18" in out
    assert "cycle 3.14" in out


def test_resolve_json(world: FakeHttp) -> None:
    code, out = run("resolve", "pypi:pydantic", "eol:node", "--json")
    assert code == 0
    data = json.loads(out)
    assert data[0]["version"] == "2.11.4" and data[0]["ecosystem"] == "pypi"
    assert data[1]["name"] == "nodejs" and data[1]["details"]["lts"] is True


def test_resolve_agent_format_is_compact(world: FakeHttp) -> None:
    code, out = run(
        "resolve", "pypi:fastapi", "gha:actions/checkout", "eol:postgres", "-f", "agent"
    )
    assert code == 0
    assert out.splitlines() == [
        "pypi:fastapi 0.121.3",
        "gha:actions/checkout v5.0.0 (uses: actions/checkout@v5)",
        "eol:postgres 18.1 (cycle 18; EOL 2099-01-01; docker postgres:18)",
    ]


def test_resolve_docker_hint_uses_a_published_tag(world: FakeHttp) -> None:
    code, out = run("resolve", "docker:elasticsearch", "eol:python", "-f", "agent")
    assert code == 0
    assert "docker elasticsearch:8.2.3" in out  # 8.2 is not published on Docker Hub
    assert "docker python:3.14" in out
    _, unverified = run("resolve", "docker:elasticsearch", "-f", "agent", "--no-verify")
    assert "docker elasticsearch:8.2)" in unverified


def test_resolve_errors_exit_nonzero(world: FakeHttp) -> None:
    code, out = run("resolve", "pypi:requests", "pypi:nope-nope", "maven:junit", "-f", "agent")
    assert code == 1
    assert "pypi:requests 2.32.5" in out
    assert "pypi:nope-nope ERROR not found on pypi" in out
    assert "maven:junit ERROR unknown ecosystem" in out


def test_resolve_default_ecosystem_and_pre(world: FakeHttp) -> None:
    code, out = run("resolve", "-e", "pypi", "fastapi", "--pre", "-f", "agent")
    assert (code, out.strip()) == (0, "fastapi 0.122.0rc1")


def test_resolve_uses_cache_then_offline(world: FakeHttp) -> None:
    run("resolve", "npm:react")
    calls = len(world.calls)
    code, out = run("resolve", "npm:react", "--offline", "-f", "agent")
    assert code == 0 and out.strip() == "npm:react 19.2.0"
    assert len(world.calls) == calls
    code, out = run("resolve", "npm:vite", "--offline", "-f", "agent")
    assert code == 1 and "offline and not in cache" in out


def test_no_cache_flag_always_fetches(world: FakeHttp) -> None:
    run("resolve", "npm:react", "--no-cache")
    run("resolve", "npm:react", "--no-cache")
    assert world.calls.count("https://registry.npmjs.org/react/latest") == 2


# --- audit --------------------------------------------------------------------------------------


def test_audit_table(world: FakeHttp) -> None:
    code, out = run("audit", str(STALE))
    assert code == 0
    assert out.splitlines()[0].split()[:4] == ["LOCATION", "CURRENT", "LATEST", "STATUS"]
    assert "actions/checkout@v3" in out and "-> actions/checkout@v5" in out
    assert "image not tracked" not in out  # skipped rows hidden without --all
    assert "checked in 7 files" in out
    _, everything = run("audit", str(STALE), "--all")
    assert "image not tracked" in everything


def test_audit_json(world: FakeHttp) -> None:
    code, out = run("audit", str(STALE / "Dockerfile"), "--json")
    assert code == 0
    data = json.loads(out)
    assert data["summary"]["files"] == 1
    first = data["findings"][0]
    assert first["current"] == "python:3.10-slim"
    assert first["suggested"] == "python:3.14-slim"
    assert first["lookup"] == "eol:python"
    assert first["file"].endswith("Dockerfile") and first["line"] == 2


def test_audit_agent_format_and_fail_on_outdated(world: FakeHttp) -> None:
    code, out = run(
        "audit",
        str(STALE / ".github" / "workflows" / "ci.yml"),
        "--format",
        "agent",
        "--fail-on-outdated",
    )
    assert code == 1
    lines = out.splitlines()
    assert lines[0].startswith("freshpin: 9 outdated of 13 checked")
    assert any(
        line.endswith("ci.yml:13 | actions/checkout@v3 -> actions/checkout@v5 [major]")
        for line in lines
    )


def test_audit_clean_project(world: FakeHttp, tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests>=2.32\n")
    code, out = run("audit", str(tmp_path), "-f", "agent", "--fail-on-outdated")
    assert code == 0
    assert out.strip() == "freshpin: OK - 1 checked, none outdated."


def test_audit_empty_and_missing_paths(world: FakeHttp, tmp_path: Path) -> None:
    code, out = run("audit", str(tmp_path))
    assert code == 0 and "No supported dependency declarations found." in out
    code, out = run("audit", str(tmp_path / "nope"))
    assert code == 2 and "no such file or directory" in out


def test_audit_ignore_and_exclude(world: FakeHttp) -> None:
    _, out = run("audit", str(STALE), "-f", "agent", "--exclude", "frontend", "--ignore", "react*")
    assert "package.json" not in out
    _, out = run("audit", str(STALE / "frontend"), "-f", "agent", "--ignore", "react*")
    assert '"react"' not in out and "@types/node" in out


def test_audit_no_floor_check(world: FakeHttp, tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("django>=4.2\n")
    assert run("audit", str(tmp_path), "--fail-on-outdated")[0] == 1
    assert run("audit", str(tmp_path), "--fail-on-outdated", "--no-floor-check")[0] == 0


def test_audit_level(world: FakeHttp, tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi==0.121.2\n")
    assert run("audit", str(tmp_path), "--fail-on-outdated")[0] == 0
    assert run("audit", str(tmp_path), "--fail-on-outdated", "--level", "patch")[0] == 1


# --- misc commands --------------------------------------------------------------------------------


def test_ecosystems(world: FakeHttp) -> None:
    code, out = run("ecosystems")
    assert code == 0 and "gha:actions/checkout" in out and "Tracked Docker images" in out
    code, out = run("ecosystems", "--json")
    data = json.loads(out)
    assert {e["ecosystem"] for e in data["ecosystems"]} == {
        "pypi",
        "npm",
        "crates",
        "gha",
        "eol",
        "docker",
    }
    assert data["docker_images"]["postgres"] == "postgresql"


def test_cache_clear(world: FakeHttp) -> None:
    run("resolve", "npm:react")
    code, out = run("cache-clear")
    assert code == 0 and out.startswith("Removed 1 cached entry")
    assert run("cache-clear")[1].startswith("Removed 0 cached entries")


def test_no_command_prints_help() -> None:
    code, out = run()
    assert code == 2 and "resolve" in out and "audit" in out


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert capsys.readouterr().out.strip() == f"freshpin {__version__}"


def test_bad_ttl_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["resolve", "pypi:x", "--ttl", "soon"])
    assert info.value.code == 2
    assert "invalid duration" in capsys.readouterr().err


def test_python_dash_m_entrypoint() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "freshpin", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0 and proc.stdout.strip() == f"freshpin {__version__}"
