#!/usr/bin/env python3
"""Claude Code ``PostToolUse`` hook: catch stale versions the moment they are written.

Wire-up (see ``hooks/hooks.json``): runs after ``Write``/``Edit``/``MultiEdit`` and ``Bash``.
When a manifest, workflow or Dockerfile was just written - by an edit tool, or by a shell
command such as ``cat > Dockerfile <<EOF`` or ``npm install react@18`` - it audits what was
written and, if any version is outdated, hands Claude a compact list of
``file:line | current -> latest`` fixes so it upgrades them immediately.

Design rules:

* It never breaks the agent loop: any unexpected problem exits 0 silently
  (set ``FRESHPIN_HOOK_DEBUG=1`` to see errors on stderr).
* It is fast: non-matching files return before freshpin is even imported, lookups are
  cached for 6h and run concurrently.
* For ``Edit``/``MultiEdit`` only the lines containing the new text are reported, so an
  intentional old pin elsewhere in the file does not nag on every edit.

Environment:
  FRESHPIN_HOOK=off          disable the hook
  FRESHPIN_HOOK_MODE=context non-blocking: add context instead of a block decision
  FRESHPIN_HOOK_TIMEOUT=4    per-request HTTP timeout in seconds
  FRESHPIN_OFFLINE=1         use cached data only
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

HOOK_DIR = Path(__file__).resolve().parent
_CANDIDATE_RE = re.compile(
    r"(?:^pyproject\.toml|requirements.*\.(?:txt|in)|^package\.json|^Cargo\.toml|\.ya?ml"
    r"|^dockerfile.*|\.dockerfile|^containerfile.*|\.containerfile)$",
    re.IGNORECASE,
)
_OFF = {"0", "off", "false", "no", "disable", "disabled"}
FOOTER = (
    "Update these to the versions shown before continuing (confirm with "
    "`freshpin resolve <ecosystem>:<name>` if unsure). If an older version is genuinely "
    "required, keep it, add a `# freshpin: ignore` comment on that line where the format "
    "allows comments, and tell the user why."
)


def plugin_root() -> Path:
    return Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or HOOK_DIR.parent)


def _import_freshpin() -> bool:
    """Make ``freshpin`` importable, preferring the copy bundled with the plugin."""
    src = plugin_root() / "src"
    if (src / "freshpin" / "__init__.py").is_file() and str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        import freshpin.auditors  # noqa: F401
    except ImportError:
        return False
    return True


def changed_lines(path: Path, tool_name: str, tool_input: Mapping[str, Any]) -> set[int] | None:
    """Line numbers touched by an Edit/MultiEdit (``None`` means the whole file)."""
    snippets: list[str] = []
    if isinstance(tool_input.get("new_string"), str):
        snippets.append(tool_input["new_string"])
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        snippets.extend(
            e["new_string"]
            for e in edits
            if isinstance(e, dict) and isinstance(e.get("new_string"), str)
        )
    if tool_name == "Write" or not snippets:
        return None
    wanted = {
        line.strip()
        for snippet in snippets
        for line in snippet.splitlines()
        if len(line.strip()) >= 3 and re.search(r"[A-Za-z0-9]", line)
    }
    if not wanted:
        return set()
    file_lines = path.read_text(encoding="utf-8").splitlines()
    return {
        i
        for i, line in enumerate(file_lines, start=1)
        if any(w in line or (line.strip() and line.strip() in w) for w in wanted)
    }


_REDIRECT_RE = re.compile(r"(?<![<>=&\d])(?:\d?>>?|&>)(?!=)\s*([^\s;&|<>()]+)")
_TEE_RE = re.compile(r"\btee\s+(?:-a\s+)?([^\s;&|<>()]+)")
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_CD_RE = re.compile(r"^\s*cd\s+([^\s;&|]+)")
_INSTALL_RE = re.compile(
    r"\b(?:(npm|pnpm|yarn|bun)\s+(?:i|install|add)|(uv|poetry|pdm)\s+add|(cargo)\s+add)\b([^;&|\n]*)"
)
_INSTALL_MANIFESTS = {
    "npm": "package.json",
    "pnpm": "package.json",
    "yarn": "package.json",
    "bun": "package.json",
    "uv": "pyproject.toml",
    "poetry": "pyproject.toml",
    "pdm": "pyproject.toml",
    "cargo": "Cargo.toml",
}
BASH_HINT_RE = re.compile(r">|\btee\b|\b(?:install|add)\b")


def _package_name(token: str) -> str:
    """``react@18`` -> ``react``; ``@types/node@20`` -> ``@types/node``; ``a>=1`` -> ``a``."""
    token = token.strip("'\"")
    if token.startswith("@"):
        head, _, _ = token[1:].partition("@")
        return "@" + head
    return re.split(r"[@=<>~!\[;]", token, maxsplit=1)[0]


def bash_targets(command: str, cwd: Path) -> tuple[list[Path], dict[Path, set[str]]]:
    """Files a shell command writes, and manifests an install command edits (with names).

    Heredoc bodies are skipped, so ``fastapi>=0.165`` inside ``cat <<EOF`` is not mistaken
    for a redirect. ``cd`` prefixes are followed so relative paths resolve correctly.
    """
    written: list[Path] = []
    installs: dict[Path, set[str]] = {}
    here = cwd
    heredoc_end: str | None = None
    for line in command.splitlines():
        if heredoc_end is not None:
            if line.strip() == heredoc_end:
                heredoc_end = None
            continue
        for segment in re.split(r"&&|\|\||;", line):
            cd = _CD_RE.match(segment)
            if cd:
                target = Path(cd.group(1).strip("'\"")).expanduser()
                here = target if target.is_absolute() else here / target
                continue
            for match in (*_REDIRECT_RE.finditer(segment), *_TEE_RE.finditer(segment)):
                raw = match.group(1).strip("'\"")
                if raw.startswith("/dev/") or not raw:
                    continue
                path = Path(raw).expanduser()
                written.append(path if path.is_absolute() else here / path)
            for match in _INSTALL_RE.finditer(segment):
                tool = match.group(1) or match.group(2) or match.group(3)
                names = {
                    _package_name(tok)
                    for tok in match.group(4).split()
                    if tok and not tok.startswith("-")
                }
                names.discard("")
                if names:
                    installs.setdefault(here / _INSTALL_MANIFESTS[tool], set()).update(names)
        doc = _HEREDOC_RE.search(line)
        if doc:
            heredoc_end = doc.group(2)
    return list(dict.fromkeys(written)), installs


def audit_in_process(path: Path, lines: set[int] | None, cwd: Path) -> str | None:
    if lines is not None and not lines:
        return None
    return audit_many([path], cwd, None if lines is None else (lambda ref: ref.line in lines))


def audit_many(
    paths: list[Path], cwd: Path, line_filter: Callable[[Any], bool] | None
) -> str | None:
    from freshpin.auditors import audit_paths
    from freshpin.cache import Cache
    from freshpin.registries import HttpClient, Resolver
    from freshpin.report import format_audit_agent

    timeout = float(os.environ.get("FRESHPIN_HOOK_TIMEOUT") or 4)
    resolver = Resolver(
        http=HttpClient(timeout=timeout, retries=1),
        cache=Cache(),
        offline=os.environ.get("FRESHPIN_OFFLINE", "").lower() in ("1", "true", "yes"),
    )
    report = audit_paths(paths, resolver, base=cwd, line_filter=line_filter)
    if not report.outdated:
        return None
    return format_audit_agent(report)


def audit_bash(command: str, cwd: Path) -> str | None:
    """Audit what a shell command just wrote (whole files) or installed (named packages)."""
    from freshpin.auditors import detect_kind

    written, installs = bash_targets(command, cwd)
    files = [p for p in written if p.is_file() and detect_kind(p) is not None]
    manifests = {p: names for p, names in installs.items() if p.is_file()}
    if not files and not manifests:
        return None
    whole = {p.resolve() for p in files}
    wanted = {p.resolve(): {n.lower() for n in names} for p, names in manifests.items()}

    def keep(ref: Any) -> bool:
        target = ref.file.resolve()
        if target in whole:
            return True
        names = wanted.get(target) or set()
        return ref.name.lower() in names or ref.display_name.lower() in names

    return audit_many(list(dict.fromkeys([*files, *manifests])), cwd, keep)


def audit_via_cli(path: Path) -> str | None:
    exe = shutil.which("freshpin")
    if exe is None:
        return None
    proc = subprocess.run(
        [exe, "audit", str(path), "--format", "agent", "--fail-on-outdated"],
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 1 and proc.stdout.strip() else None


def emit(message: str, out: TextIO) -> None:
    text = f"{message}\n{FOOTER}"
    if os.environ.get("FRESHPIN_HOOK_MODE", "").lower() == "context":
        payload: dict[str, Any] = {
            "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}
        }
    else:
        payload = {"decision": "block", "reason": text}
    out.write(json.dumps(payload) + "\n")


def run(stdin: TextIO, stdout: TextIO) -> int:
    if os.environ.get("FRESHPIN_HOOK", "").lower() in _OFF:
        return 0
    try:
        payload = json.load(stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    cwd = Path(payload.get("cwd") or Path.cwd())
    if payload.get("tool_name") == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or not BASH_HINT_RE.search(command):
            return 0  # fast path: the command cannot have written a manifest
        if not _import_freshpin():
            return 0
        message = audit_bash(command, cwd)
        if message:
            emit(message, stdout)
        return 0
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return 0
    path = Path(file_path)
    if not path.is_absolute():
        path = cwd / path
    if not _CANDIDATE_RE.search(path.name) or not path.is_file():
        return 0  # fast path: not a file freshpin understands

    if _import_freshpin():
        from freshpin.auditors import detect_kind

        if detect_kind(path) is None:
            return 0
        lines = changed_lines(path, str(payload.get("tool_name") or ""), tool_input)
        message = audit_in_process(path, lines, cwd)
    else:
        message = audit_via_cli(path)
    if message:
        emit(message, stdout)
    return 0


def main() -> int:
    try:
        return run(sys.stdin, sys.stdout)
    except Exception as exc:  # never break Claude's flow because of the hook
        if os.environ.get("FRESHPIN_HOOK_DEBUG"):
            print(f"freshpin hook error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
