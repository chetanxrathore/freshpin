from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from conftest import FakeHttp
from freshpin import __version__
from freshpin.cache import Cache
from freshpin.registries import Resolver
from freshpin.registries.endoflife import EndOfLifeRegistry

ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = ROOT / "hooks" / "check_written_file.py"


def load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("freshpin_hook", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = load_hook()


@pytest.fixture
def world(patch_http: Callable[..., FakeHttp]) -> FakeHttp:
    return patch_http()


def call(payload: Any) -> tuple[int, dict[str, Any] | None]:
    stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    stdout = io.StringIO()
    code = hook.run(stdin, stdout)
    text = stdout.getvalue().strip()
    return code, (json.loads(text) if text else None)


def write_payload(path: Path, cwd: Path | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "session_id": "s",
        "hook_event_name": "PostToolUse",
        "tool_name": extra.pop("tool_name", "Write"),
        "cwd": str(cwd or path.parent),
        "tool_input": {"file_path": str(path), **extra},
        "tool_response": {"filePath": str(path), "type": "create"},
    }


@pytest.fixture
def dockerfile(tmp_path: Path) -> Path:
    path = tmp_path / "Dockerfile"
    path.write_text("FROM python:3.10-slim\nRUN echo hi\nFROM postgres:14-alpine\n")
    return path


def test_write_of_stale_file_blocks_with_actionable_reason(
    world: FakeHttp, dockerfile: Path
) -> None:
    code, out = call(write_payload(dockerfile))
    assert code == 0 and out is not None
    assert out["decision"] == "block"
    reason = out["reason"]
    assert reason.startswith("freshpin: 2 outdated of 2 checked")
    assert "Dockerfile:1 | python:3.10-slim -> python:3.14-slim [minor]" in reason
    assert "Dockerfile:3 | postgres:14-alpine -> postgres:18-alpine [major] 14 is EOL" in reason
    assert "freshpin: ignore" in reason  # escape hatch is explained


def test_edit_only_reports_lines_just_written(world: FakeHttp, dockerfile: Path) -> None:
    payload = write_payload(
        dockerfile,
        tool_name="Edit",
        old_string="FROM postgres:13",
        new_string="FROM postgres:14-alpine",
    )
    _, out = call(payload)
    assert out is not None
    assert "postgres:14-alpine" in out["reason"]
    assert "python:3.10-slim" not in out["reason"]


def test_multiedit_and_deletions(world: FakeHttp, dockerfile: Path) -> None:
    multi = write_payload(
        dockerfile,
        tool_name="MultiEdit",
        edits=[{"old_string": "x", "new_string": "python:3.10-slim"}],
    )
    _, out = call(multi)
    assert out is not None and "python:3.10-slim" in out["reason"]
    assert "postgres" not in out["reason"]
    deletion = write_payload(dockerfile, tool_name="Edit", old_string="RUN echo hi", new_string="")
    assert call(deletion) == (0, None)


def test_up_to_date_file_is_silent(world: FakeHttp, tmp_path: Path) -> None:
    path = tmp_path / "Dockerfile"
    path.write_text("FROM python:3.14-slim\n")
    assert call(write_payload(path)) == (0, None)


def test_relative_paths_resolve_against_cwd(world: FakeHttp, tmp_path: Path) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    wf = tmp_path / ".github" / "workflows" / "ci.yml"
    wf.write_text("jobs:\n  t:\n    steps:\n      - uses: actions/checkout@v3\n")
    payload = write_payload(wf, cwd=tmp_path)
    payload["tool_input"]["file_path"] = ".github/workflows/ci.yml"
    _, out = call(payload)
    assert out is not None
    assert (
        ".github/workflows/ci.yml:4 | actions/checkout@v3 -> actions/checkout@v5 [major]"
        in (out["reason"])
    )


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        json.dumps({"tool_name": "Write"}),
        json.dumps({"tool_input": "x"}),
        json.dumps({"tool_input": {"file_path": ""}}),
        json.dumps({"tool_input": {"file_path": "/definitely/missing/package.json"}}),
    ],
)
def test_bad_or_irrelevant_payloads_are_ignored(world: FakeHttp, payload: str) -> None:
    assert call(payload) == (0, None)


def test_non_manifest_files_are_ignored(world: FakeHttp, tmp_path: Path) -> None:
    for name in ("main.py", "config.yml", "notes.txt"):
        path = tmp_path / name
        path.write_text("FROM python:3.8\nuses: actions/checkout@v1\n")
        assert call(write_payload(path)) == (0, None)
    assert world.calls == []


def test_can_be_disabled(
    world: FakeHttp, dockerfile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRESHPIN_HOOK", "off")
    assert call(write_payload(dockerfile)) == (0, None)


def test_context_mode(world: FakeHttp, dockerfile: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRESHPIN_HOOK_MODE", "context")
    _, out = call(write_payload(dockerfile))
    assert out is not None and "decision" not in out
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "python:3.14-slim" in out["hookSpecificOutput"]["additionalContext"]


def test_main_never_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_: Any) -> int:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(hook, "run", boom)
    assert hook.main() == 0
    monkeypatch.setenv("FRESHPIN_HOOK_DEBUG", "1")
    assert hook.main() == 0
    assert "kaboom" in capsys.readouterr().err


def test_cli_fallback_when_package_is_not_importable(
    dockerfile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hook, "_import_freshpin", lambda: False)
    monkeypatch.setattr(hook.shutil, "which", lambda name: "/usr/local/bin/freshpin")

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert cmd[1:] == ["audit", str(dockerfile), "--format", "agent", "--fail-on-outdated"]
        return subprocess.CompletedProcess(cmd, 1, stdout="freshpin: 1 outdated\nx | a -> b\n")

    monkeypatch.setattr(hook.subprocess, "run", fake_run)
    _, out = call(write_payload(dockerfile))
    assert out is not None and out["reason"].startswith("freshpin: 1 outdated")
    monkeypatch.setattr(hook.shutil, "which", lambda name: None)
    assert call(write_payload(dockerfile)) == (0, None)


def test_changed_lines(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("requests==2.28.0\nfastapi==0.100.0\n]\n")
    assert hook.changed_lines(path, "Write", {"content": "x"}) is None
    assert hook.changed_lines(path, "Edit", {"new_string": "fastapi==0.100.0"}) == {2}
    assert hook.changed_lines(path, "Edit", {"new_string": "0.100"}) == {2}
    assert hook.changed_lines(path, "Edit", {"new_string": "]"}) == set()


def test_script_end_to_end_offline_from_cache(tmp_path: Path) -> None:
    """Run the real script in a subprocess, served entirely from a pre-seeded cache."""
    cache_dir = tmp_path / "cache"
    resolver = Resolver(
        http=FakeHttp(),
        cache=Cache(cache_dir / "cache.json"),
        registries={"eol": EndOfLifeRegistry(today=date(2026, 11, 26))},
    )
    assert resolver.resolve("eol", "python").ok
    project = tmp_path / "proj"
    project.mkdir()
    (project / "Dockerfile").write_text("FROM python:3.10-slim\n")
    env = {
        **os.environ,
        "FRESHPIN_CACHE_DIR": str(cache_dir),
        "FRESHPIN_OFFLINE": "1",
        "CLAUDE_PLUGIN_ROOT": str(ROOT),
    }
    env.pop("FRESHPIN_HOOK", None)
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(write_payload(project / "Dockerfile")),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert "Dockerfile:1 | python:3.10-slim -> python:3.14-slim" in out["reason"]


# --- plugin packaging ------------------------------------------------------------------------


def test_plugin_manifests_are_consistent() -> None:
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert plugin["name"] == "freshpin"
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", plugin["name"])
    assert "hooks" not in plugin and "skills" not in plugin  # auto-discovered defaults
    entry = market["plugins"][0]
    assert entry["name"] == plugin["name"] and entry["source"] == "./"
    assert entry["version"] == plugin["version"] == market["metadata"]["version"]
    assert plugin["version"] == __version__, "bump .claude-plugin/*.json with __version__"
    assert market["owner"]["name"]


def test_hooks_json_points_at_the_hook_script() -> None:
    config = json.loads((ROOT / "hooks" / "hooks.json").read_text())
    (entry,) = config["hooks"]["PostToolUse"]
    assert set(entry["matcher"].split("|")) >= {"Write", "Edit", "MultiEdit", "Bash"}
    (command,) = entry["hooks"]
    assert command["type"] == "command" and isinstance(command["timeout"], int)
    assert "${CLAUDE_PLUGIN_ROOT}/hooks/freshpin-hook.sh" in command["command"]
    assert HOOK_PATH.is_file() and os.access(HOOK_PATH, os.X_OK)
    assert os.access(ROOT / "hooks" / "freshpin-hook.sh", os.X_OK)


def test_skill_frontmatter() -> None:
    text = (ROOT / "skills" / "freshpin" / "SKILL.md").read_text()
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md must start with YAML frontmatter"
    fields = dict(line.split(": ", 1) for line in match.group(1).splitlines())
    assert fields["name"] == "freshpin"
    assert 50 < len(fields["description"]) <= 1024
    for trigger in ("Dockerfile", "docker-compose.yml", ".github/workflows", "package.json"):
        assert trigger in fields["description"]
    assert "freshpin resolve" in text and "freshpin audit" in text


def test_skill_launcher_runs_bundled_copy() -> None:
    script = ROOT / "skills" / "freshpin" / "scripts" / "freshpin.py"
    proc = subprocess.run(
        [sys.executable, "-I", str(script), "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("freshpin ")


# --- Bash: files written by shell commands and manifests edited by installs ----------------


def bash_payload(command: str, cwd: Path) -> dict[str, Any]:
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "cwd": str(cwd),
        "tool_input": {"command": command, "description": "x"},
        "tool_response": {"stdout": "", "stderr": ""},
    }


def test_bash_targets_parsing(tmp_path: Path) -> None:
    command = (
        "cd sub && mkdir -p .github/workflows\n"
        "cat > requirements.txt <<'E'\nfastapi>=0.100.0\nuvicorn>=0.30\nE\n"
        "cat <<EOF > Dockerfile\nFROM python:3.10-slim\nEOF\n"
        "echo x | tee -a docker-compose.yml 2>/dev/null; echo y >> /abs/.github/workflows/ci.yml\n"
        "npm install react@18 @types/node@20 -D && uv add 'fastapi>=0.100' httpx; cargo add serde@1"
    )
    written, installs = hook.bash_targets(command, tmp_path)
    sub = tmp_path / "sub"
    assert written == [
        sub / "requirements.txt",
        sub / "Dockerfile",
        sub / "docker-compose.yml",
        Path("/abs/.github/workflows/ci.yml"),
    ]
    assert installs == {
        sub / "package.json": {"react", "@types/node"},
        sub / "pyproject.toml": {"fastapi", "httpx"},
        sub / "Cargo.toml": {"serde"},
    }
    # heredoc bodies are not commands: ">=" inside them is not a redirect
    assert not any(str(p).endswith("0.100.0") for p in written)


def test_bash_heredoc_write_is_audited(world: FakeHttp, tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python:3.10-slim\n")
    command = "cat > Dockerfile <<'EOF'\nFROM python:3.10-slim\nEOF"
    _, out = call(bash_payload(command, tmp_path))
    assert out is not None and out["decision"] == "block"
    assert "Dockerfile:1 | python:3.10-slim -> python:3.14-slim" in out["reason"]


def test_bash_install_only_reports_named_packages(world: FakeHttp, tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"react": "^18.2.0", "next": "14.2.3"}}'
    )
    _, out = call(bash_payload("npm install react@18.2.0", tmp_path))
    assert out is not None
    assert '"react": "^18.2.0"' in out["reason"] and "next" not in out["reason"]


def test_bash_without_writes_is_free(world: FakeHttp, tmp_path: Path) -> None:
    for command in ("ls -la", "pytest -q", "git status", "echo hi > notes.txt", "pip list"):
        assert call(bash_payload(command, tmp_path)) == (0, None)
    assert world.calls == []


def _broken_python_dir(tmp_path: Path) -> Path:
    """A PATH entry whose `python3` fails to start, like a non-system Python in a sandbox."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "python3"
    fake.write_text(
        "#!/bin/sh\necho 'Fatal Python error: Failed to import encodings' >&2\nexit 1\n"
    )
    fake.chmod(0o755)
    return bindir


def test_sh_launchers_skip_a_broken_python(tmp_path: Path) -> None:
    working = Path(sys.executable).parent
    env = {**os.environ, "PATH": f"{_broken_python_dir(tmp_path)}:{working}:/usr/bin:/bin"}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    script = ROOT / "skills" / "freshpin" / "scripts" / "freshpin"
    proc = subprocess.run(
        ["/bin/sh", str(script), "--version"], capture_output=True, text=True, env=env, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("freshpin ")
    hook_sh = ROOT / "hooks" / "freshpin-hook.sh"
    proc = subprocess.run(
        ["/bin/sh", str(hook_sh)],
        input="not json",
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0 and proc.stdout == ""


def test_sh_hook_without_any_python_is_silent(tmp_path: Path) -> None:
    env = {"PATH": str(_broken_python_dir(tmp_path)) + ":/nonexistent"}
    proc = subprocess.run(
        ["/bin/sh", str(ROOT / "hooks" / "freshpin-hook.sh")],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    # /usr/bin/python3 may exist on the host; either way the hook must exit 0 quietly
    assert proc.returncode == 0 and proc.stdout == ""
