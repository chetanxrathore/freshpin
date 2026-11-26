# Publishing freshpin

freshpin ships through two channels, and one release covers both:

1. **PyPI**: the `freshpin` package, which installs the `freshpin` CLI.
2. **GitHub**: the repository is also a Claude Code plugin marketplace (skill + hook).

Releases are automated. Pushing a `v*` tag runs [`.github/workflows/release.yml`](.github/workflows/release.yml), which:

- builds the wheel and sdist;
- checks the tag matches the package version;
- installs the wheel on Linux, macOS and Windows with Python 3.9 and 3.15;
- publishes to PyPI with [Trusted Publishing](https://docs.pypi.org/trusted-publishers/), so no token is stored, and attaches signed PEP 740 attestations;
- creates the GitHub release with the built files.

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs lint, types, plugin validation and the tests on every push and pull request.

Run every command from the repository root.

---

## 0. One-time prerequisites

- A GitHub account.
- A [PyPI](https://pypi.org/account/register/) account with two-factor authentication enabled.
- [uv](https://docs.astral.sh/uv/) installed.
- Optional: a [TestPyPI](https://test.pypi.org/account/register/) account (separate from PyPI) for a manual rehearsal, and the Claude Code CLI (`claude`) to validate the plugin locally.

## 1. Check the project metadata

The repository is set up to live at **https://github.com/chetanxrathore/freshpin**. That URL appears in:

- `README.md` (badges, install and CI commands)
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

## 2. Verify locally

These are the same checks CI runs:

```bash
uv run ruff check src tests hooks skills validation
```

```bash
uv run ruff format --check src tests hooks skills
```

```bash
uv run mypy --strict src/freshpin
```

```bash
uv run pytest -v
```

```bash
claude plugin validate . --strict
```

Live checks against the real registries, which CI does not run:

```bash
uv run freshpin resolve pypi:fastapi gha:actions/checkout eol:postgres eol:python
```

```bash
uv run --with packaging python validation/accuracy.py
```

Optionally, also run the A/B agent eval. It costs about $5–15 of Claude usage, and the full command with its network grants is in `evals/README.md`.

## 3. Commit and push to GitHub

Commit the work:

```bash
git add -A
```

```bash
git commit -m "Rename to freshpin; add CI, release workflow, validation, evals and roadmap"
```

Create an **empty** public repository named `freshpin` on GitHub (web UI → *New repository*, with no README, license or .gitignore). Then connect it. This uses the same SSH host alias as your other repositories:

```bash
git remote add origin git@github.com-chetanxrathore:chetanxrathore/freshpin.git
```

(Over HTTPS instead: `git remote add origin https://github.com/chetanxrathore/freshpin.git`.)

```bash
git push -u origin main
```

Open the repository's *Actions* tab and wait for **CI** to go green. The *freshpin audits itself* job is allowed to fail; it only warns when one of this repo's own actions or tools has a newer release.

## 4. Test the Claude Code plugin locally (optional)

Open Claude Code in another project and run:

```text
/plugin marketplace add /absolute/path/to/freshpin
/plugin install freshpin@freshpin
```

Then ask Claude to "write a Dockerfile for a Python app using python:3.10". The hook should reply with `python:3.10-slim -> python:3.15-slim`, or whatever is current, and Claude should fix the file.

## 5. One-time: connect PyPI to the release workflow

**On PyPI**, register a *pending publisher*. It turns into a normal Trusted Publisher once the first release creates the project:

1. Go to <https://pypi.org/manage/account/publishing/> and choose *GitHub*.
2. Fill in exactly:

   | Field | Value |
   |---|---|
   | PyPI Project Name | `freshpin` |
   | Owner | `chetanxrathore` |
   | Repository name | `freshpin` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

**On GitHub**, create the matching environment:

1. Repository → *Settings → Environments → New environment*, name it `pypi`.
2. Recommended protections:
   - *Required reviewers*: add yourself, so every release waits for your click before uploading.
   - *Deployment branches and tags*: *Selected branches and tags*, add a **tag** rule `v*`.

## 6. Dry run (optional)

- **Without publishing:** *Actions → Release → Run workflow* on `main`. This builds and runs the install smoke tests on all three OSes. The publish and GitHub release jobs only run for `v*` tags, so they are skipped. The same jobs run on any pull request that changes `pyproject.toml`, `src/freshpin/__init__.py` or the workflow.
- **Manual TestPyPI rehearsal:** build locally (`rm -rf dist && uv build`), create a TestPyPI API token with scope *Entire account*, then:

  ```bash
  uv publish --publish-url https://test.pypi.org/legacy/ --token "pypi-TEST-TOKEN-HERE"
  ```

  ```bash
  uvx --default-index https://test.pypi.org/simple/ freshpin --version
  ```

  This works because freshpin has no dependencies, so nothing else has to come from TestPyPI.

## 7. Release

```bash
git tag -a v0.1.0 -m "freshpin 0.1.0"
```

```bash
git push origin v0.1.0
```

In *Actions → Release*, follow the run. If you added a required reviewer, approve the `pypi` deployment when it asks. When the run finishes, freshpin 0.1.0 is on PyPI and has a GitHub release with the wheel, the sdist and generated release notes.

## 8. Verify the release

```bash
uvx freshpin --version
```

```bash
uvx freshpin resolve pypi:freshpin
```

The second command should print the version you just released. It may take a minute to appear. The PyPI page's *Download files* section shows each file's provenance, and the GitHub attestation can be checked against a downloaded file:

```bash
gh attestation verify freshpin-0.1.0-py3-none-any.whl --repo chetanxrathore/freshpin
```

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
2. Commit, push, and wait for CI to pass.
3. Tag `vX.Y.Z` and push the tag, as in step 7. The workflow refuses to publish if the tag and the version disagree.

## Fallback: upload by hand

If GitHub Actions is unavailable, you can upload from your machine:

```bash
rm -rf dist && uv build
```

```bash
uvx twine check --strict dist/*
```

```bash
uv publish --token "pypi-REAL-TOKEN-HERE"
```

For the very first upload the token's scope must be *Entire account*. Replace it with a token scoped to `freshpin` right afterwards. Files uploaded this way have no attestations.

## Troubleshooting

| Error | Cause / fix |
|---|---|
| `invalid-publisher: valid token, but no corresponding publisher` | The pending publisher on PyPI doesn't match the workflow exactly. Check owner `chetanxrathore`, repository `freshpin`, workflow `release.yml`, environment `pypi`. |
| Publish job waits forever | The `pypi` environment has a required reviewer. Approve the deployment in the run's page. |
| *Check tag matches package version* fails | The tag (for example `v0.2.0`) doesn't match `__version__`. Delete the tag (`git tag -d v0.2.0`, `git push origin :refs/tags/v0.2.0`), fix the version, commit, and tag again. |
| Publish succeeds but nothing new appears on PyPI | `skip-existing` is on, so a version that is already on PyPI is silently skipped. PyPI never accepts the same version twice, even after you delete it. Bump the version. |
| `gh release create` says the release already exists | A release for that tag was created by hand. Delete it on GitHub, then re-run the failed job. |
| `403 Invalid or non-existent authentication` (manual upload) | Wrong index for the token (TestPyPI and PyPI tokens are separate), or the token was revoked. |
| `The name 'freshpin' is too similar to an existing project` | PyPI rejected the name. On 2026-11-26, `freshpin` was unused on PyPI, TestPyPI, npm, crates.io and Homebrew. If this happens, pick a new name: replace `freshpin` and `FRESHPIN` everywhere, and rename `src/freshpin`, `skills/freshpin` and `skills/freshpin/scripts/freshpin.py`. |
| Plugin installs but the hook does nothing | The hook needs a Python 3.9+; `hooks/freshpin-hook.sh` tries `python3`, `/usr/bin/python3` and `python3.X`. Run it by hand with `FRESHPIN_HOOK_DEBUG=1` to see errors. |
