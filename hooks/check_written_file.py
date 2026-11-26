#!/usr/bin/env python3
"""Claude Code ``PostToolUse`` hook: catch stale versions the moment they are written.

Wire-up (see ``hooks/hooks.json``): runs after ``Write``/``Edit``/``MultiEdit``. When the
edited file is a manifest, workflow or Dockerfile, it audits the lines that were just
written and, if any version is outdated, hands Claude a compact list of
``file:line | current -> latest`` fixes so it upgrades them immediately.

Design rules:

* It never breaks the agent loop: any unexpected problem exits 0 silently
  (set ``FRESHDEPS_HOOK_DEBUG=1`` to see errors on stderr).
* It is fast: non-matching files return before freshdeps is even imported, lookups are
  cached for 6h and run concurrently.
* For ``Edit``/``MultiEdit`` only the lines containing the new text are reported, so an
  intentional old pin elsewhere in the file does not nag on every edit.

Environment:
  FRESHDEPS_HOOK=off          disable the hook
  FRESHDEPS_HOOK_MODE=context non-blocking: add context instead of a block decision
  FRESHDEPS_HOOK_TIMEOUT=4    per-request HTTP timeout in seconds
  FRESHDEPS_OFFLINE=1         use cached data only
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
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
    "`freshdeps resolve <ecosystem>:<name>` if unsure). If an older version is genuinely "
    "required, keep it, add a `# freshdeps: ignore` comment on that line where the format "
    "allows comments, and tell the user why."
)


def plugin_root() -> Path:
    return Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or HOOK_DIR.parent)


def _import_freshdeps() -> bool:
    """Make ``freshdeps`` importable, preferring the copy bundled with the plugin."""
    src = plugin_root() / "src"
    if (src / "freshdeps" / "__init__.py").is_file() and str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        import freshdeps.auditors  # noqa: F401
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


def audit_in_process(path: Path, lines: set[int] | None, cwd: Path) -> str | None:
    from freshdeps.auditors import audit_paths
    from freshdeps.cache import Cache
    from freshdeps.registries import HttpClient, Resolver
    from freshdeps.report import format_audit_agent

    if lines is not None and not lines:
        return None
    timeout = float(os.environ.get("FRESHDEPS_HOOK_TIMEOUT") or 4)
    resolver = Resolver(
        http=HttpClient(timeout=timeout, retries=1),
        cache=Cache(),
        offline=os.environ.get("FRESHDEPS_OFFLINE", "").lower() in ("1", "true", "yes"),
    )
    report = audit_paths(
        [path],
        resolver,
        base=cwd,
        line_filter=None if lines is None else (lambda ref: ref.line in lines),
    )
    if not report.outdated:
        return None
    return format_audit_agent(report)


def audit_via_cli(path: Path) -> str | None:
    exe = shutil.which("freshdeps")
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
    if os.environ.get("FRESHDEPS_HOOK_MODE", "").lower() == "context":
        payload: dict[str, Any] = {
            "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}
        }
    else:
        payload = {"decision": "block", "reason": text}
    out.write(json.dumps(payload) + "\n")


def run(stdin: TextIO, stdout: TextIO) -> int:
    if os.environ.get("FRESHDEPS_HOOK", "").lower() in _OFF:
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
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return 0
    cwd = Path(payload.get("cwd") or Path.cwd())
    path = Path(file_path)
    if not path.is_absolute():
        path = cwd / path
    if not _CANDIDATE_RE.search(path.name) or not path.is_file():
        return 0  # fast path: not a file freshdeps understands

    if _import_freshdeps():
        from freshdeps.auditors import detect_kind

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
        if os.environ.get("FRESHDEPS_HOOK_DEBUG"):
            print(f"freshdeps hook error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
