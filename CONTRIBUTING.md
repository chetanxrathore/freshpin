# Contributing to freshpin

Thanks for helping agents stop shipping stale versions. This guide covers the dev setup, the project rules and how to add a registry or an auditor.

## Setup

```bash
uv sync
```

This creates `.venv` with the dev tools (pytest, ruff, mypy) and installs freshpin in editable mode.

## Before you open a pull request

All four must pass:

```bash
uv run ruff check src tests hooks
```

```bash
uv run ruff format --check src tests hooks
```

```bash
uv run mypy --strict src/freshpin
```

```bash
uv run pytest
```

## Live validation and agent evals (optional, need network)

```bash
uv run --with packaging python validation/accuracy.py
```

```bash
uv run python validation/corpus.py --workdir /tmp/freshpin-corpus
```

`accuracy.py` compares freshpin's answers with independent sources such as `npm view`, the PyPI simple index, `git ls-remote` and the official release feeds. It also checks that suggested Docker tags exist. `corpus.py` audits popular open-source repositories, looking for crashes, parse errors and suspicious suggestions. The A/B agent eval suite is in `evals/` (see `evals/README.md`); it needs a signed-in `claude`.

## Ground rules

- **Zero runtime dependencies.** freshpin must stay installable anywhere in a second, so use the standard library only (`urllib.request`, `json`, `re`, `concurrent.futures`...). Dev tools are fine in the `dev` dependency group.
- **Python 3.9+.** macOS's built-in Python is 3.9, and Claude Code's sandbox may allow nothing else, so the hook and skill must run there:
  - No `tomllib`, `typing.Self`, `StrEnum`, `datetime.UTC`, `match` statements or `zip(strict=...)`.
  - Mypy 2.x can only target 3.10, so also run the suite on 3.9:
    ```bash
    uv run --no-project --python 3.9 --with "pytest<9" -- env PYTHONPATH=src python -m pytest
    ```
- **Strict typing.** `mypy --strict` must stay clean.
- **Tests never touch the network.** `tests/conftest.py` blocks `urlopen` and provides `FakeHttp` plus a canned "registry world". Add payloads there, using dates far in the past or future so tests don't depend on today's date.
- **Never break the agent loop.** Registry and parse failures become `error` or `skipped` findings, never exceptions. The hook always exits 0.
- **Keep agent output terse.** `--format agent` is read by LLMs, so every token counts.

## Adding a registry

1. Create `src/freshpin/registries/<name>.py` with a `Registry` subclass. Set `ecosystem`, `aliases`, `title` and `example`, and implement `fetch()`. It returns `{"version": ..., "url": ..., "details": {...}}`, filters out pre-releases unless `include_pre` is set, and raises `RegistryError` on failure.
2. Register it in `REGISTRIES` in `src/freshpin/registries/__init__.py`.
3. Add mocked payloads to `tests/conftest.py` and tests to `tests/test_registries.py`.

## Adding an auditor

1. Create `src/freshpin/auditors/<name>.py` with a `parse_<name>(path, text) -> list[DependencyRef]` function. Record exact line numbers, a `template` showing how the declaration reads (with `{spec}` where the version goes), and the right `kind` so the shared evaluator interprets the constraint correctly.
2. Hook it into `PARSERS` and `detect_kind()` in `src/freshpin/auditors/__init__.py`. If the hook should react to the new file name, also update `_CANDIDATE_RE` in `hooks/check_written_file.py`.
3. Add a stale fixture under `tests/fixtures/` and expectations in `tests/test_auditors.py`.

## Commit style

Use small, focused commits with an imperative subject line ("Add Maven registry"), and explain *why* in the body when it isn't obvious.
