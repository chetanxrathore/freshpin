---
name: freshdeps
description: Look up the latest stable versions of libraries, GitHub Actions, Docker base images, databases and runtimes from live registries instead of recalling them from training data. Use when scaffolding a project, adding or upgrading dependencies, or writing or editing pyproject.toml, requirements.txt, package.json, Cargo.toml, Dockerfile, docker-compose.yml or .github/workflows/*.yml. Also use when choosing a version of Python, Node.js, Go, Rust, PostgreSQL, MySQL, Redis, MongoDB, Ubuntu, Debian, Alpine, nginx or any GitHub Action (actions/checkout, setup-python, setup-node...).
allowed-tools: Bash(freshdeps *), Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/freshdeps.py *)
---

# freshdeps: never guess a version

Your training data is months or years old. Versions you remember, like `actions/checkout@v3`, `postgres:14`, `python:3.10`, `"react": "^18.2.0"` or `node:18`, are often outdated or end-of-life. **Do not write a version number from memory.** Look it up first.

## Workflow

1. **Before writing or editing any manifest, workflow, Dockerfile or compose file**, resolve every package, action, image and runtime you will mention. Do it in **one** call:

   ```bash
   freshdeps resolve pypi:fastapi pypi:pydantic npm:next crates:tokio gha:actions/checkout eol:postgres eol:python --format agent
   ```

2. **Use the versions it returns.** Keep the constraint style the project already uses (`>=`, `^`, `~=`, exact pins), but base it on the resolved version.
   - GitHub Actions: use the major tag it prints (`uses: actions/checkout@v7`). If the repo pins actions by commit SHA, keep doing that and put the version in a comment.
   - Docker images: use the `docker <image>:<tag>` hint (for example `postgres:19`, `python:3.15-slim`). Keep the variant suffix the project already uses (`-slim`, `-alpine`).
   - Runtimes with LTS lines (Node.js, Ubuntu, MySQL, Temurin) resolve to the newest **LTS** release. Other runtimes resolve to the newest supported release.
3. **Before you finish**, audit what you changed and fix every line it reports:

   ```bash
   freshdeps audit . --format agent
   ```

   Each line reads `file:line | current -> suggested [bump] note`. Apply the suggestion, then run the audit again until it prints `freshdeps: OK`.

## When an older version is correct

Keep an older version only when there is a real reason. For example, the user asked for it, a peer dependency requires it, or the platform does not support the newer one yet. When you keep one, say why in your reply, and add a `# freshdeps: ignore` comment on that line if the file format allows comments. JSON does not.

## Running the CLI

Try these in order:

1. `freshdeps ...` if it is on PATH.
2. `python3 ${CLAUDE_SKILL_DIR}/scripts/freshdeps.py ...`, which runs the copy bundled with the freshdeps plugin and needs no install.
3. If neither works, ask the user to install it (see the project README). Do **not** install a package called `freshdeps` from PyPI: that name belongs to an unrelated project.

If the CLI is unavailable and you cannot install it, query the registries directly and apply the same rules (stable releases only, no pre-releases):

| Kind | Endpoint | Field |
|---|---|---|
| PyPI | `https://pypi.org/pypi/<pkg>/json` | `info.version` |
| npm | `https://registry.npmjs.org/<pkg>/latest` | `version` |
| crates.io | `https://crates.io/api/v1/crates/<crate>` | `crate.max_stable_version` |
| GitHub Action | `https://api.github.com/repos/<owner>/<repo>/releases/latest` | `tag_name` |
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

Useful flags: `--json` for machine-readable output, `--pre` to include pre-releases, `--offline` to use cached data only, `--no-cache` to force fresh lookups. `freshdeps ecosystems` lists every alias.

## What `audit` checks

- `pyproject.toml`: `[project]` dependencies, optional dependencies, dependency groups, `build-system.requires`, Poetry tables, and `requires-python`, which is flagged when it targets an EOL Python.
- `requirements*.txt`
- `package.json`: all dependency sections and `engines.node`.
- `Cargo.toml`: all dependency tables.
- `.github/workflows/*.yml` and `action.yml`: `uses:` refs, service and container images, and `python-version` / `node-version` / `go-version`.
- `Dockerfile`: `FROM`, including `ARG` defaults.
- `docker-compose.yml` / `compose.yaml`: `image:`.
