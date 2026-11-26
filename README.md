# freshdeps

**Stop AI agents from writing `actions/checkout@v3` and `postgres:14`.**

AI coding agents pick versions from training data that is months or years old. So they scaffold projects with GitHub Actions three majors behind, Docker images that are already end-of-life, and `"react": "^18.2.0"` long after 19 shipped. Nothing breaks right away, which is why it keeps happening.

`freshdeps` makes the agent **look versions up instead of recalling them**:

| Layer | What it does |
|---|---|
| **CLI** (`freshdeps`) | Queries PyPI, npm, crates.io, GitHub and endoflife.date concurrently, caches the results for 6 hours, and works offline from the cache. Pure Python standard library, so it has zero dependencies. |
| **Skill** (`skills/freshdeps/SKILL.md`) | Tells Claude Code never to guess a version: run `freshdeps resolve` before writing a manifest, and `freshdeps audit` before finishing. |
| **Hook** (`hooks/`) | A `PostToolUse` hook. When Claude writes a stale version into a manifest, workflow or Dockerfile, the hook returns the fix right away and Claude upgrades it. |

```text
$ freshdeps audit . --format agent
freshdeps: 11 outdated of 12 checked - upgrade to the latest stable versions:
.github/workflows/ci.yml:5 | actions/checkout@v3 -> actions/checkout@v7 [major]
.github/workflows/ci.yml:6 | actions/setup-node@v3 -> actions/setup-node@v7 [major]
.github/workflows/ci.yml:8 | node-version: "18" -> node-version: "26" [major] 18 is EOL since 2025-04-30
Dockerfile:1 | python:3.10-slim -> python:3.15-slim [minor] 3.10 is EOL since 2026-10-01
docker-compose.yml:3 | postgres:14 -> postgres:19 [major] 14 is EOL since 2026-11-12
docker-compose.yml:5 | redis:6.2 -> redis:8.12 [major]
package.json:3 | "next": "14.2.3" -> "next": "16.4.6" [major] constraint excludes the latest release
package.json:4 | "react": "^18.2.0" -> "react": "^19.3.1" [major] constraint excludes the latest release
pyproject.toml:3 | requires-python = ">=3.9" -> requires-python = ">=3.11" [minor] 3.9 is EOL since 2025-10-31
pyproject.toml:4 | fastapi>=0.100.0 -> fastapi>=0.165.0 [minor] floor is a breaking release behind
pyproject.toml:4 | pydantic>=1.10,<2 -> pydantic>=2.14.4 [major] constraint excludes the latest release
```

<sub>Example output from November 2026. You will see whatever is current when you run it.</sub>

---

## Install

> In the commands below, replace `<owner>` with the GitHub account or organisation that hosts this repository.

### Claude Code (plugin: skill + hook)

```text
/plugin marketplace add <owner>/freshdeps
/plugin install freshdeps@freshdeps
```

The plugin ships with its own copy of the CLI. Both the skill and the hook work without any extra install; they only need `python3` 3.10 or newer on your `PATH`.

**Skill only, no plugin:** copy the folder and install the CLI (below):

```bash
cp -r skills/freshdeps ~/.claude/skills/freshdeps
```

### Standalone CLI

```bash
uv tool install git+https://github.com/<owner>/freshdeps
```

```bash
pipx install git+https://github.com/<owner>/freshdeps
```

Once a release is on PyPI, install the **`freshdeps-cli`** distribution with `uv tool install freshdeps-cli` or `pipx install freshdeps-cli`. The command it installs is still `freshdeps`.

> ⚠️ Do **not** `pip install freshdeps`. That PyPI name belongs to an unrelated project.

### Cursor, Codex, Gemini CLI and other agents

Install the CLI (above). Then add the rule to the instructions file your agent reads: `AGENTS.md` for Codex and Cursor, `GEMINI.md` for Gemini CLI.

```bash
printf '\n## Dependency versions\nNever write a library, GitHub Action, Docker image, database or runtime version from memory. Before writing or editing a manifest, Dockerfile, compose file or workflow, run `freshdeps resolve <ecosystem>:<name> ... --format agent` and use the versions it returns. Before finishing, run `freshdeps audit . --format agent` and fix every line it reports.\n' >> AGENTS.md
```

Agents that support the Agent Skills format (`SKILL.md`) can also load `skills/freshdeps/` directly. Copy it into that agent's skills directory.

### CI

Fail the build when something is outdated:

```yaml
- uses: actions/checkout@v7
- uses: astral-sh/setup-uv@v10
- run: uvx --from git+https://github.com/<owner>/freshdeps freshdeps audit . --fail-on-outdated
  env:
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}  # raises the GitHub API rate limit
```

---

## Usage

### `freshdeps resolve`: latest stable versions, live

```text
$ freshdeps resolve pypi:fastapi pypi:pydantic npm:next crates:tokio gha:actions/checkout eol:postgres eol:python eol:node docker:redis
QUERY                 LATEST   NOTES
pypi:fastapi          0.165.0
pypi:pydantic         2.14.4
npm:next              16.4.6
crates:tokio          1.54.1
gha:actions/checkout  v7.0.1   uses: actions/checkout@v7
eol:postgres          19.0     cycle 19 · EOL 2031-11-19 · docker postgres:19
eol:python            3.15.2   cycle 3.15 · EOL 2031-10-31 · docker python:3.15
eol:node              26.13.1  cycle 26 LTS · EOL 2029-04-30 · docker node:26
docker:redis          8.12.0   cycle 8.12 · no EOL date yet · docker redis:8.12
```

`--format agent` prints compact, low-token output for LLMs. `--json` prints machine-readable output.

```text
$ freshdeps resolve pypi:fastapi gha:actions/checkout eol:postgres --format agent
pypi:fastapi 0.165.0
gha:actions/checkout v7.0.1 (uses: actions/checkout@v7)
eol:postgres 19.0 (cycle 19; EOL 2031-11-19; docker postgres:19)
```

### `freshdeps audit`: find stale versions in a project

```text
$ freshdeps audit
LOCATION                    CURRENT                    LATEST   STATUS            ACTION / NOTE
.github/workflows/ci.yml:5  actions/checkout@v3        v7.0.1   outdated (major)  -> actions/checkout@v7
Dockerfile:1                python:3.10-slim           3.15.2   outdated (minor)  -> python:3.15-slim  3.10 is EOL since 2026-10-01
docker-compose.yml:3        postgres:14                19.0     outdated (major)  -> postgres:19  14 is EOL since 2026-11-12
package.json:4              "react": "^18.2.0"         19.3.1   outdated (major)  -> "react": "^19.3.1"  constraint excludes the latest release
pyproject.toml:4            requests>=2.31             2.34.2   ok
...
12 checked in 5 files: 11 outdated, 1 ok
```

| Flag | Meaning |
|---|---|
| `--format agent` / `--json` | Compact `file:line \| current -> latest` lines for agents, or JSON for tools. |
| `--fail-on-outdated` | Exit with status 1 if anything is outdated. Use it in CI and hooks. |
| `--level major\|minor\|patch` | Smallest bump to report. The default is `minor`, so patch-only drift is ignored. |
| `--ignore NAME` | Skip a dependency by name or glob. Repeatable. |
| `--exclude GLOB` | Skip paths, e.g. `--exclude 'examples/*'`. Repeatable. |
| `--all` | Also list skipped declarations, such as git or path dependencies and untracked images. |
| `--offline` / `--no-cache` / `--ttl 30m` / `--pre` / `--timeout 5` | Lookup behaviour. These also work with `resolve`. |

To keep an old version on purpose, add `# freshdeps: ignore` on that line in TOML, YAML, Dockerfiles or requirements files.

### The `PostToolUse` hook in action

Say Claude writes this `Dockerfile`:

```dockerfile
FROM python:3.10-slim
FROM postgres:14-alpine
```

The hook replies right away and Claude updates the file before moving on:

```json
{"decision": "block", "reason": "freshdeps: 2 outdated of 2 checked - upgrade to the latest stable versions:\nDockerfile:1 | python:3.10-slim -> python:3.15-slim [minor] 3.10 is EOL since 2026-10-01\nDockerfile:2 | postgres:14-alpine -> postgres:19-alpine [major] 14 is EOL since 2026-11-12\nUpdate these to the versions shown before continuing ..."}
```

How the hook behaves:

- It runs only on supported files. Everything else returns immediately, before freshdeps is even imported.
- After an `Edit`, it reports only the lines that edit wrote, so a deliberate old pin elsewhere in the file won't nag on every edit.
- It never breaks the agent loop: any internal error exits 0 silently. Set `FRESHDEPS_HOOK_DEBUG=1` to see errors.
- `FRESHDEPS_HOOK=off` disables it. `FRESHDEPS_HOOK_MODE=context` adds the warning as context instead of a blocking decision.

---

## Supported ecosystems

| Prefix | Source | Notes |
|---|---|---|
| `pypi:` | `https://pypi.org/pypi/<pkg>/json` | Stable releases only. Excludes `a`/`b`/`rc`/`dev` and fully yanked releases. |
| `npm:` | `https://registry.npmjs.org/<pkg>/latest` | Scoped packages work (`npm:@types/node`). Deprecations are reported. |
| `crates:` | `https://crates.io/api/v1/crates/<crate>` | Uses `max_stable_version`. |
| `gha:` | `https://api.github.com/repos/<owner>/<repo>/releases/latest` | Falls back to tags. Uses `GITHUB_TOKEN`/`GH_TOKEN` when set. Also works for any GitHub release. |
| `eol:` | `https://endoflife.date/api/<product>.json` | Accepts any endoflife.date product, plus aliases (`postgres`, `node`, `golang`, `mongo`, `alpine`...). |
| `docker:` | endoflife.date, by image name | `python`, `node`, `postgres`, `mysql`, `mariadb`, `redis`, `valkey`, `mongo`, `golang`, `rust`, `ruby`, `php`, `ubuntu`, `debian`, `alpine`, `nginx`, `httpd`, `rabbitmq`, `elasticsearch`, `eclipse-temurin`, ... (`freshdeps ecosystems` lists them all) |

| File | What is checked |
|---|---|
| `pyproject.toml` | `[project]` dependencies, optional dependencies, `[dependency-groups]`, `build-system.requires`, `tool.uv.dev-dependencies`, Poetry tables, `requires-python` |
| `requirements*.txt`, `requirements*.in`, `requirements/*.txt` | Every requirement line, including hash-pinned and continued lines |
| `package.json` | `dependencies`, `devDependencies`, `optionalDependencies`, `peerDependencies`, `npm:` aliases, `engines.node` |
| `Cargo.toml` | `[dependencies]`, `[dev-dependencies]`, `[build-dependencies]`, `[workspace.dependencies]`, `[target.*.dependencies]`, `[dependencies.<crate>]`, renamed packages |
| `.github/workflows/*.yml`, `action.yml` | `uses:` (including SHA pins with a `# vX.Y.Z` comment), `docker://` actions, service and container images, `python-version` / `node-version` / `go-version`, matrices (flags only EOL entries) |
| `Dockerfile`, `*.Dockerfile`, `Containerfile` | `FROM`, with `ARG` defaults substituted. Build stages and `scratch` are skipped. |
| `docker-compose*.yml`, `compose*.yaml` | `image:`, including `${VAR:-default}` |

### How "outdated" is decided

- **Version constraints** (PEP 440, npm, Cargo, Poetry) are outdated when they **exclude** the latest stable release (`^18.2.0` vs 19.x, `<2`, `==2.28.0`). They are also outdated when they allow it but their floor is a **breaking** release behind (`fastapi>=0.100` while 0.165 is out, `django>=4.2` while 6.x is out). A floor that is only a minor or patch behind, like `requests>=2.31`, is fine.
- **Runtime floors** (`requires-python`, `engines.node`, Poetry `python`) are outdated when they still target an **end-of-life** release or exclude the current one. The suggestion is the oldest *supported* release.
- **Pinned images and runtimes** (`python:3.12-slim`, `node-version: 20`) are compared at the precision you wrote (`postgres:18` floats within 18) and are always flagged when **EOL**.
- **LTS policy:** for products with LTS lines (Node.js, Ubuntu, MySQL, Temurin...) freshdeps recommends the newest **LTS** release. Everything else gets the newest supported release.
- **GitHub Actions** are compared at the tag's precision: `@v4` against the latest major, `@v4.1.1` against the full version.

### Configuration

| Variable | Default | |
|---|---|---|
| `FRESHDEPS_CACHE_DIR` | `$XDG_CACHE_HOME/freshdeps` or `~/.cache/freshdeps` | Cache location (`cache.json`). |
| `FRESHDEPS_CACHE_TTL` | `6h` | How long results stay fresh, e.g. `30m`, `1d`. Expired entries are still used as an offline fallback. |
| `GITHUB_TOKEN` / `GH_TOKEN` | unset | Raises the GitHub API limit above 60 requests per hour. |
| `FRESHDEPS_OFFLINE` | unset | Hook only: use cached data and never touch the network. |
| `FRESHDEPS_HOOK`, `FRESHDEPS_HOOK_MODE`, `FRESHDEPS_HOOK_TIMEOUT`, `FRESHDEPS_HOOK_DEBUG` | | Hook controls, described above. |
| `NO_COLOR` | unset | Disable coloured table output. |

`freshdeps cache-clear` empties the cache. `freshdeps ecosystems` lists every prefix, alias and tracked Docker image.

**Exit codes:** `0` OK · `1` outdated found (with `--fail-on-outdated`) or a `resolve` lookup failed · `2` usage error.

---

## Development

```bash
uv sync
```

```bash
uv run ruff check src tests hooks
```

```bash
uv run mypy --strict src/freshdeps
```

```bash
uv run pytest -v
```

The test suite mocks every network call, so it is fast, deterministic and works offline. See [CONTRIBUTING.md](CONTRIBUTING.md) for adding a registry or an auditor.

## License

[MIT](LICENSE-MIT)
