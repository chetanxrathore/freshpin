# Publishing freshpin

freshpin ships through two channels, and one release covers both:

1. **PyPI**: the `freshpin` package, which installs the `freshpin` CLI.
2. **GitHub**: the repository is also a Claude Code plugin marketplace (skill + hook).

Run every command from the repository root. Nothing here runs automatically; each step is manual.

---

## 0. One-time prerequisites

- A GitHub account.
- A [PyPI](https://pypi.org/account/register/) account **and** a separate [TestPyPI](https://test.pypi.org/account/register/) account, both with two-factor authentication enabled. PyPI requires 2FA to upload.
- [uv](https://docs.astral.sh/uv/) installed.
- Optional: the Claude Code CLI (`claude`), to validate the plugin.

## 1. Check the project metadata

The repository is set up to live at **https://github.com/chetanxrathore/freshpin**. That URL appears in:

- `README.md` (install and CI commands)
- `pyproject.toml` → `[project.urls]` (links on the PyPI page)
- `.claude-plugin/plugin.json` → `homepage` and `repository`

If you host it under a different account, replace `chetanxrathore` in those files and in the commands below:

```bash
grep -rn "chetanxrathore" --exclude-dir=.git .
```

Optionally, set a real author name in these places, which currently say "freshpin contributors":

- `pyproject.toml` → `authors`
- `LICENSE-MIT` → copyright line
- `.claude-plugin/plugin.json` → `author.name`
- `.claude-plugin/marketplace.json` → `owner.name`

## 2. Verify before releasing

All of these must pass:

```bash
uv run ruff check src tests hooks skills
```

```bash
uv run mypy --strict src/freshpin
```

```bash
uv run pytest -v
```

```bash
uv run freshpin resolve pypi:fastapi gha:actions/checkout eol:postgres eol:python
```

```bash
uv run --with packaging python validation/accuracy.py
```

Optionally, also run the A/B agent eval. It costs about $5–15 of Claude usage, and the full command with its network grants is in `evals/README.md`.

## 3. Commit and push to GitHub

Commit the rename and any placeholder edits:

```bash
git add -A
```

```bash
git commit -m "Rename project to freshpin"
```

Create an **empty** public repository named `freshpin` on GitHub (web UI → *New repository*, with no README, license or .gitignore). Then connect it and push:

```bash
git remote add origin https://github.com/chetanxrathore/freshpin.git
```

```bash
git push -u origin main
```

## 4. Validate the Claude Code plugin

If the Claude Code CLI is installed:

```bash
claude plugin validate . --strict
```

To test the plugin locally before announcing it, open Claude Code in another project and run:

```text
/plugin marketplace add /absolute/path/to/freshpin
/plugin install freshpin@freshpin
```

Then ask Claude to "write a Dockerfile for a Python app using python:3.10". The hook should reply with `python:3.10-slim -> python:3.15-slim`, or whatever is current, and Claude should fix the file.

## 5. Build the release artifacts

```bash
rm -rf dist && uv build
```

```bash
uvx twine check --strict dist/*
```

This produces `dist/freshpin-<version>-py3-none-any.whl` and `dist/freshpin-<version>.tar.gz`.

## 6. Rehearse on TestPyPI

1. On TestPyPI, go to *Account settings → API tokens* and create a token. On a first upload it must have scope *Entire account*, because the project doesn't exist there yet.
2. Upload:

   ```bash
   uv publish --publish-url https://test.pypi.org/legacy/ --token "pypi-TEST-TOKEN-HERE"
   ```

3. Install the package from TestPyPI into a throwaway environment and run it:

   ```bash
   uvx --default-index https://test.pypi.org/simple/ freshpin --version
   ```

   ```bash
   uvx --default-index https://test.pypi.org/simple/ freshpin resolve pypi:requests eol:python
   ```

   This works because freshpin has no dependencies, so nothing else has to come from TestPyPI.

## 7. Publish to PyPI

1. On PyPI, go to *Account settings → API tokens* and create a token. On the first upload its scope must be *Entire account*.
2. Upload:

   ```bash
   uv publish --token "pypi-REAL-TOKEN-HERE"
   ```

   You can also export `UV_PUBLISH_TOKEN` instead of passing `--token`.
3. **Right after the first upload**, delete the account-wide token and create a new one scoped to the `freshpin` project only. Use the scoped token for future releases.
4. Verify from the real index:

   ```bash
   uvx freshpin --version
   ```

   ```bash
   uvx freshpin resolve pypi:freshpin
   ```

   The second command should print the version you just published. It may take a minute to appear.

## 8. Tag the release on GitHub

```bash
git tag -a v0.1.0 -m "freshpin 0.1.0"
```

```bash
git push origin v0.1.0
```

On GitHub, go to *Releases → Draft a new release*, choose tag `v0.1.0`, and attach the two files from `dist/`.

## 9. Tell users how to install

- **Claude Code plugin:** `/plugin marketplace add chetanxrathore/freshpin`, then `/plugin install freshpin@freshpin`.
- **CLI:** `uv tool install freshpin` or `pipx install freshpin`.

---

## Releasing a new version later

1. Update the version in **three** files. `pyproject.toml` reads it from `__init__.py` automatically, and `tests/test_hook.py` fails if they disagree.
   - `src/freshpin/__init__.py` → `__version__`
   - `.claude-plugin/plugin.json` → `version`
   - `.claude-plugin/marketplace.json` → `metadata.version` and `plugins[0].version`

   Claude Code only offers plugin users an update when the plugin `version` changes.
2. Repeat steps 2, 3 (commit and push), 5, 7 and 8 with the new version number.

## Optional: automate with Trusted Publishing

Once the manual flow works, you can let GitHub Actions publish without storing a token. See [PyPI Trusted Publishers](https://docs.pypi.org/trusted-publishers/). Add a *pending publisher* for `freshpin` on PyPI and use `uv publish --trusted-publishing always` in the workflow. Before you commit the workflow, run `freshpin audit .github/workflows` so its actions are current.

## Troubleshooting

| Error | Cause / fix |
|---|---|
| `400 File already exists` | PyPI never accepts the same version twice, even after you delete it. Bump the version and rebuild. |
| `403 Invalid or non-existent authentication` | Wrong index for the token (TestPyPI and PyPI tokens are separate), or the token was revoked. |
| `The name 'freshpin' is too similar to an existing project` | PyPI rejected the name. On 2026-11-26, `freshpin` was unused on PyPI, TestPyPI, npm, crates.io and Homebrew. If this happens, pick a new name: replace `freshpin` and `FRESHPIN` everywhere, and rename `src/freshpin`, `skills/freshpin` and `skills/freshpin/scripts/freshpin.py`. |
| Plugin installs but the hook does nothing | The hook needs a Python 3.9+; `hooks/freshpin-hook.sh` tries `python3`, `/usr/bin/python3` and `python3.X`. Run it by hand with `FRESHPIN_HOOK_DEBUG=1` to see errors. |
