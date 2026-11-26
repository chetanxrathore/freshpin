# Contributing to freshdeps

Thanks for helping agents stop shipping stale versions. This guide covers the dev setup, the project rules and how to add a registry or an auditor.

## Setup

```bash
uv sync
```

This creates `.venv` with the dev tools (pytest, ruff, mypy) and installs freshdeps in editable mode.

## Before you open a pull request

All four must pass:

```bash
uv run ruff check src tests hooks
```

```bash
uv run ruff format --check src tests hooks
```

```bash
uv run mypy --strict src/freshdeps
```

```bash
uv run pytest
```

## Ground rules

- **Zero runtime dependencies.** freshdeps must stay installable anywhere in a second, so use the standard library only (`urllib.request`, `json`, `re`, `concurrent.futures`...). Dev tools are fine in the `dev` dependency group.
- **Python 3.10+.** No `tomllib`, `typing.Self`, `StrEnum` or `datetime.UTC`. Mypy is configured for 3.10 and catches most of these.
- **Strict typing.** `mypy --strict` must stay clean.
- **Tests never touch the network.** `tests/conftest.py` blocks `urlopen` and provides `FakeHttp` plus a canned "registry world". Add payloads there, using dates far in the past or future so tests don't depend on today's date.
- **Never break the agent loop.** Registry and parse failures become `error` or `skipped` findings, never exceptions. The hook always exits 0.
- **Keep agent output terse.** `--format agent` is read by LLMs, so every token counts.

## Adding a registry

1. Create `src/freshdeps/registries/<name>.py` with a `Registry` subclass. Set `ecosystem`, `aliases`, `title` and `example`, and implement `fetch()`. It returns `{"version": ..., "url": ..., "details": {...}}`, filters out pre-releases unless `include_pre` is set, and raises `RegistryError` on failure.
2. Register it in `REGISTRIES` in `src/freshdeps/registries/__init__.py`.
3. Add mocked payloads to `tests/conftest.py` and tests to `tests/test_registries.py`.

## Adding an auditor

1. Create `src/freshdeps/auditors/<name>.py` with a `parse_<name>(path, text) -> list[DependencyRef]` function. Record exact line numbers, a `template` showing how the declaration reads (with `{spec}` where the version goes), and the right `kind` so the shared evaluator interprets the constraint correctly.
2. Hook it into `PARSERS` and `detect_kind()` in `src/freshdeps/auditors/__init__.py`. If the hook should react to the new file name, also update `_CANDIDATE_RE` in `hooks/check_written_file.py`.
3. Add a stale fixture under `tests/fixtures/` and expectations in `tests/test_auditors.py`.

## Commit style

Use small, focused commits with an imperative subject line ("Add Maven registry"), and explain *why* in the body when it isn't obvious.
