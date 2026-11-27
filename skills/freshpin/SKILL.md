---
name: freshpin
description: Look up the latest stable versions of libraries, GitHub Actions, Docker base images, databases and runtimes from live registries instead of recalling them from training data. Use when scaffolding a project, adding or upgrading dependencies, or writing or editing pyproject.toml, requirements.txt, package.json, Cargo.toml, Dockerfile, docker-compose.yml or .github/workflows/*.yml. Also use when choosing a version of Python, Node.js, Go, Rust, PostgreSQL, MySQL, Redis, MongoDB, Ubuntu, Debian, Alpine, nginx or any GitHub Action (actions/checkout, setup-python, setup-node...).
allowed-tools: Bash(freshpin *), Bash(sh ${CLAUDE_SKILL_DIR}/scripts/freshpin *)
---

# freshpin: never guess a version

Your training data is months or years old. Versions you remember, like `actions/checkout@v3`, `postgres:14`, `python:3.10`, `"react": "^18.2.0"` or `node:18`, are often outdated or end-of-life. **Do not write a version number from memory.** Look it up first.

## Workflow

1. **Before writing or editing any manifest, workflow, Dockerfile or compose file**, resolve every package, action, image and runtime you will mention. Do it in **one** call:

   ```bash
   freshpin resolve pypi:fastapi pypi:pydantic npm:next crates:tokio gha:actions/checkout eol:postgres eol:python --format agent
   ```

2. **Use the versions it returns.** Keep the constraint style the project already uses (`>=`, `^`, `~=`, exact pins), but base it on the resolved version.
   - GitHub Actions: use exactly the ref it prints. That is usually the major tag (`uses: actions/checkout@v7`), but some actions publish no major tag, and then it prints the full tag (`uses: astral-sh/setup-uv@v10.4.0`). Never shorten it. If the repo pins actions by commit SHA, keep doing that and put the version in a comment. `freshpin audit` prints the exact SHA to pin.
   - Docker images: use the `docker <image>:<tag>` hint (for example `postgres:19`, `python:3.15-slim`). These tags have been checked on Docker Hub. Keep the variant suffix the project already uses (`-slim`, `-alpine`), but not a retired OS release (`-buster`, `-focal`); the audit suggests a current one.
   - Runtimes with LTS lines (Node.js, Ubuntu, MySQL, Temurin) resolve to the newest **LTS** release. Other runtimes resolve to the newest supported release.
3. **Before you finish**, audit what you changed and fix every line it reports:

   ```bash
   freshpin audit . --format agent
   ```

   Each line reads `file:line | current -> suggested [bump] note`. Apply the suggestion, then run the audit again until it prints `freshpin: OK`. An `unverified:` line names versions freshpin could not check (the registry was unreachable). Confirm those with `freshpin resolve <ecosystem>:<name>` before you finish.
   - `[missing]` means the exact version you pinned **does not exist** on the registry, so installing it fails. It was almost certainly recalled wrongly. Always replace it with the suggested version; never keep it or ignore it. The suggestion stays on the same major version when one exists. If the note says the suggestion is a breaking upgrade and the project must stay on its current major, use the newest release of that major instead (the note says how to find it).
   - `[yanked]` means the maintainers withdrew that exact version (yanked on PyPI or crates.io, deprecated on npm), usually because it is broken or unsafe. Move to the suggested version, even when that is a downgrade.

## When an older version is correct

Keep an older version only when there is a real reason. For example, the user asked for it, a peer dependency requires it, or the platform does not support the newer one yet. When you keep one, say why in your reply, and add a `# freshpin: ignore` comment on that line if the file format allows comments. JSON does not. This never applies to a `[missing]` version, and a `[yanked]` one needs the user's explicit say-so.

## Running the CLI

Try these in order:

1. `freshpin ...` if it is on PATH.
2. `sh ${CLAUDE_SKILL_DIR}/scripts/freshpin ...`, which runs the copy bundled with the freshpin plugin. It needs no install and picks a Python 3.9+ that works, even in a sandbox.
3. If neither works, ask the user to install it (see the project README). Do not install it yourself without the user's approval.

If the CLI is unavailable and you cannot install it, query the registries directly and apply the same rules (stable releases only, no pre-releases):

| Kind | Endpoint | Field |
|---|---|---|
| PyPI | `https://pypi.org/pypi/<pkg>/json` | `info.version` |
| npm | `https://registry.npmjs.org/<pkg>/latest` | `version` |
| crates.io | `https://crates.io/api/v1/crates/<crate>` | `crate.max_stable_version` |
| GitHub Action | `https://github.com/<owner>/<repo>/releases/latest` (redirects to the tag), or `git ls-remote --tags https://github.com/<owner>/<repo>` | tag in the redirect URL; highest stable tag. Use the short `vN` only if `git ls-remote` lists `refs/tags/vN` or `refs/heads/vN`; otherwise use the full tag |
| Runtime / DB / OS | `https://endoflife.date/api/<product>.json` | first non-EOL cycle (prefer `lts`) |

## Query syntax

| Prefix | Looks up | Example |
|---|---|---|
| `pypi:` | Python package | `pypi:django` |
| `npm:` | npm package (scoped names work) | `npm:@types/node` |
| `crates:` | Rust crate | `crates:serde` |
| `gha:` | GitHub Action or any GitHub release | `gha:actions/setup-python` |
| `eol:` | runtime, database or OS ([endoflife.date](https://endoflife.date) slug or alias) | `eol:postgres`, `eol:node`, `eol:ubuntu` |
| `docker:` | Docker image, mapped to its product | `docker:redis`, `docker:golang` |

Useful flags: `--json` for machine-readable output, `--pre` to include pre-releases, `--offline` to use cached data only, `--no-cache` to force fresh lookups. For a library's own dependency ranges, use `audit --no-floor-check` so that only ranges excluding the latest release are reported. `freshpin ecosystems` lists every alias.

## What `audit` checks

- `pyproject.toml`: `[project]` dependencies, optional dependencies, dependency groups, `build-system.requires`, Poetry tables, and `requires-python`, which is flagged when it targets an EOL Python.
- `requirements*.txt`
- `package.json`: all dependency sections and `engines.node`.
- `Cargo.toml`: all dependency tables.
- `.github/workflows/*.yml` and `action.yml`: `uses:` refs, service and container images, and `python-version` / `node-version` / `go-version`.
- `Dockerfile`: `FROM`, including `ARG` defaults.
- `docker-compose.yml` / `compose.yaml`: `image:`.
