# freshpin

**Stop AI agents from writing `actions/checkout@v3` and `postgres:14`.**

AI coding agents pick versions from training data that is months or years old. So they scaffold projects with GitHub Actions three majors behind, Docker images that are already end-of-life, and `"react": "^18.2.0"` long after 19 shipped. Nothing breaks right away, which is why it keeps happening.

`freshpin` makes the agent **look versions up instead of recalling them**:

| Layer | What it does |
|---|---|
| **CLI** (`freshpin`) | Queries PyPI, npm, crates.io, GitHub and endoflife.date concurrently, caches the results for 6 hours, and works offline from the cache. Pure Python standard library, so it has zero dependencies. |
| **Skill** (`skills/freshpin/SKILL.md`) | Tells Claude Code never to guess a version: run `freshpin resolve` before writing a manifest, and `freshpin audit` before finishing. |
| **Hook** (`hooks/`) | A `PostToolUse` hook. When Claude writes a stale version into a manifest, workflow or Dockerfile, the hook returns the fix right away and Claude upgrades it. |

```text
$ freshpin audit . --format agent
freshpin: 11 outdated of 12 checked - upgrade to the latest stable versions:
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

### Why trust it

Every suggestion is checked against live sources, not guessed:

- **GitHub Actions** versions come from the repository's latest release, and the commit SHA comes from its git tags. SHA-pinned workflows get the exact SHA to pin.
- **Docker image tags** are confirmed on Docker Hub before they are suggested. If a short tag isn't published (`elasticsearch:9.5`), freshpin falls back to the full version (`9.5.7`). If an OS variant is retired (`python:3.15-buster`), it switches to a current one (`-trixie`).
- **Runtimes with LTS lines** (Node.js, Ubuntu, MySQL, Java) get the newest *LTS* release, never a pre-release.

Measured on 2026-11-26 (details in [docs/VALIDATION.md](docs/VALIDATION.md)):

- **It changes what the agent writes.** In an A/B `claude plugin eval`, Claude Sonnet 5.5 scaffolded a FastAPI and a Next.js project with and without freshpin. With freshpin, **39/39** version checks were current; without it, **0/39**. From memory it wrote `python:3.13`, `postgres:17`, `redis:7`, `node:24`, `actions/checkout@v4`/`@v5` and `typescript ^5.9`. With freshpin it wrote `python:3.15`, `postgres:19`, `redis:8.12`, `node:26`, `@v7` and `^7.0.2`.

- **100% agreement with independent sources:** 95/95 versions match across PyPI, npm, crates.io, GitHub and the official release feeds, and 43/43 suggested Docker tags exist on Docker Hub.
- **Robust on real projects:** across 18 popular open-source repositories, freshpin parsed 1,321 files and 12,135 version declarations with 0 crashes and 0 parse errors. The audit took about 4 seconds with a warm cache.
- **Tested on every supported Python:** 307 offline tests pass on Python 3.9 through 3.15, including macOS's built-in 3.9, with 94% branch coverage.
- **The plugin passes Claude Code's validator:** `claude plugin validate --strict` reports nothing. The A/B eval suite ships in [`evals/`](evals/).

---

## Install

### Claude Code (plugin: skill + hook)

```text
/plugin marketplace add chetanxrathore/freshpin
/plugin install freshpin@freshpin
```

The plugin ships with its own copy of the CLI. Both the skill and the hook work without any extra install; they only need a Python 3.9 or newer. The built-in `/usr/bin/python3` on macOS is enough; the launchers pick a working interpreter automatically.

**Skill only, no plugin:** copy the folder and install the CLI (below):

```bash
cp -r skills/freshpin ~/.claude/skills/freshpin
```

### Standalone CLI

```bash
uv tool install git+https://github.com/chetanxrathore/freshpin
```

```bash
pipx install git+https://github.com/chetanxrathore/freshpin
```

Once a release is on PyPI (see [PUBLISHING.md](PUBLISHING.md)):

```bash
uv tool install freshpin
```

```bash
pipx install freshpin
```

### Cursor, Codex, Gemini CLI and other agents

Install the CLI (above). Most agents now load the Agent Skills format (`SKILL.md`), so copy `skills/freshpin/` into the agent's skills directory:

| Agent | Project skills directory | User skills directory |
|---|---|---|
| Codex | `.agents/skills/` | `~/.agents/skills/` |
| Gemini CLI | `.gemini/skills/` (or `.agents/skills/`) | `~/.gemini/skills/` |
| Cursor | `.cursor/skills/` (also reads `.agents/` and `.claude/`) | `~/.cursor/skills/` |
| GitHub Copilot / VS Code | `.github/skills/` | `~/.copilot/skills/` |
| JetBrains Junie | `.junie/skills/` | `~/.junie/skills/` |

These paths come from each agent's documentation as of November 2026.

Skills are only advice. Agents without hooks may skip them, so also add a rule to the instructions file your agent reads: `AGENTS.md` for Codex and Cursor, or `GEMINI.md` for Gemini CLI.

```bash
printf '\n## Dependency versions\nNever write a library, GitHub Action, Docker image, database or runtime version from memory. Before writing or editing a manifest, Dockerfile, compose file or workflow, run `freshpin resolve <ecosystem>:<name> ... --format agent` and use the versions it returns. Before finishing, run `freshpin audit . --format agent` and fix every line it reports.\n' >> AGENTS.md
```


### CI

Fail the build when something is outdated:

```yaml
- uses: actions/checkout@v7
- uses: astral-sh/setup-uv@v10
- run: uvx --from git+https://github.com/chetanxrathore/freshpin freshpin audit . --fail-on-outdated
  env:
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}  # optional: freshpin also works without a token
```

---

## Usage

### `freshpin resolve`: latest stable versions, live

```text
$ freshpin resolve pypi:fastapi pypi:pydantic npm:next crates:tokio gha:actions/checkout eol:postgres eol:python eol:node docker:redis
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
$ freshpin resolve pypi:fastapi gha:actions/checkout eol:postgres --format agent
pypi:fastapi 0.165.0
gha:actions/checkout v7.0.1 (uses: actions/checkout@v7)
eol:postgres 19.0 (cycle 19; EOL 2031-11-19; docker postgres:19)
```

### `freshpin audit`: find stale versions in a project

```text
$ freshpin audit
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
| `--no-floor-check` | Library mode: report only constraints that **exclude** the latest release, not ranges whose lower bound is merely old (`packaging>=20`). |
| `--no-verify` | Skip the Docker Hub check on suggested image tags. |
| `--offline` / `--no-cache` / `--ttl 30m` / `--pre` / `--timeout 5` | Lookup behaviour. These also work with `resolve`. |

To keep an old version on purpose, add `# freshpin: ignore` on that line in TOML, YAML, Dockerfiles or requirements files.

### The `PostToolUse` hook in action

Say Claude writes this `Dockerfile`:

```dockerfile
FROM python:3.10-slim
FROM postgres:14-alpine
```

The hook replies right away and Claude updates the file before moving on:

```json
{"decision": "block", "reason": "freshpin: 2 outdated of 2 checked - upgrade to the latest stable versions:\nDockerfile:1 | python:3.10-slim -> python:3.15-slim [minor] 3.10 is EOL since 2026-10-01\nDockerfile:2 | postgres:14-alpine -> postgres:19-alpine [major] 14 is EOL since 2026-11-12\nUpdate these to the versions shown before continuing ..."}
```

How the hook behaves:

- It runs after `Write`/`Edit`/`MultiEdit` **and after `Bash`**:
  - Files written by shell commands (`cat > Dockerfile <<EOF`, `tee`, `>`) are audited.
  - So are packages added with `npm install`, `pnpm/yarn/bun add`, `uv/poetry/pdm add` and `cargo add`.
  - Agents write files through the shell more often than you'd think; the agent eval caught exactly that.
- It runs only on supported files and commands. Everything else returns immediately, before freshpin is even imported.
- After an `Edit`, it reports only the lines that edit wrote, so a deliberate old pin elsewhere in the file won't nag on every edit.
- It never breaks the agent loop: any internal error exits 0 silently. Set `FRESHPIN_HOOK_DEBUG=1` to see errors.
- `FRESHPIN_HOOK=off` disables it. `FRESHPIN_HOOK_MODE=context` adds the warning as context instead of a blocking decision.

---

## Supported ecosystems

| Prefix | Source | Notes |
|---|---|---|
| `pypi:` | `https://pypi.org/pypi/<pkg>/json` | Stable releases only. Excludes `a`/`b`/`rc`/`dev` and fully yanked releases. |
| `npm:` | `https://registry.npmjs.org/<pkg>/latest` | Scoped packages work (`npm:@types/node`). Deprecations are reported. |
| `crates:` | `https://crates.io/api/v1/crates/<crate>` | Uses `max_stable_version`. |
| `gha:` | `github.com/<owner>/<repo>/releases/latest` and git refs (no API quota); REST API when `GITHUB_TOKEN`/`GH_TOKEN` is set | Each path falls back to the other. Returns the tag and its commit SHA. Works for any GitHub release, not only Actions. |
| `eol:` | `https://endoflife.date/api/<product>.json` | Accepts any endoflife.date product, plus aliases (`postgres`, `node`, `golang`, `mongo`, `alpine`...). |
| `docker:` | endoflife.date, by image name | `python`, `node`, `postgres`, `mysql`, `mariadb`, `redis`, `valkey`, `mongo`, `golang`, `rust`, `ruby`, `php`, `ubuntu`, `debian`, `alpine`, `nginx`, `httpd`, `rabbitmq`, `elasticsearch`, `eclipse-temurin`, ... (`freshpin ecosystems` lists them all) |

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
- **LTS policy:** for products with LTS lines (Node.js, Ubuntu, MySQL, Temurin...) freshpin recommends the newest **LTS** release. Everything else gets the newest supported release.
- **GitHub Actions** are compared at the tag's precision: `@v4` against the latest major, `@v4.1.1` against the full version.

### Configuration

| Variable | Default | |
|---|---|---|
| `FRESHPIN_CACHE_DIR` | `$XDG_CACHE_HOME/freshpin` or `~/.cache/freshpin` | Cache location (`cache.json`). |
| `FRESHPIN_CACHE_TTL` | `6h` | How long results stay fresh, e.g. `30m`, `1d`. Expired entries are still used as an offline fallback. |
| `GITHUB_TOKEN` / `GH_TOKEN` | unset | Optional. When set, freshpin asks the GitHub REST API first; without it, it uses the token-free web and git endpoints. |
| `FRESHPIN_OFFLINE` | unset | Hook only: use cached data and never touch the network. |
| `FRESHPIN_HOOK`, `FRESHPIN_HOOK_MODE`, `FRESHPIN_HOOK_TIMEOUT`, `FRESHPIN_HOOK_DEBUG` | | Hook controls, described above. |
| `NO_COLOR` | unset | Disable coloured table output. |

`freshpin cache-clear` empties the cache. `freshpin ecosystems` lists every prefix, alias and tracked Docker image.

**Exit codes:** `0` OK · `1` outdated found (with `--fail-on-outdated`) or a `resolve` lookup failed · `2` usage error.

---

## How freshpin compares

| | Fixes stale versions **while the agent writes** | Packages | GitHub Actions | Docker images | EOL runtimes / DBs | Token or account needed |
|---|---|---|---|---|---|---|
| **freshpin** | ✅ PostToolUse hook + skill | PyPI, npm, crates.io | ✅ (incl. SHA pins) | ✅ verified tags | ✅ LTS-aware | No |
| Renovate / Dependabot | ❌ PRs after the commit | many | ✅ | ✅ | partial | Bot setup |
| npm-check-updates, taze, pur, `uv tree --outdated` | ❌ manual CLI | one each | taze only | ❌ | ❌ | No |
| Version MCP servers (e.g. package-version-check-mcp) | only if the model calls them | many | some | some | ❌ | No |
| Security MCPs (Snyk, Socket, Sonatype, Endor) | advisory, security-focused | many | ❌ | partial | ❌ | Yes |
| pinact / actions-up | ❌ manual or CI | ❌ | ✅ | ❌ | ❌ | No |

The full landscape study is in [docs/RESEARCH.md](docs/RESEARCH.md), and what comes next is in [ROADMAP.md](ROADMAP.md).

## Development

```bash
uv sync
```

```bash
uv run ruff check src tests hooks
```

```bash
uv run mypy --strict src/freshpin
```

```bash
uv run pytest -v
```

The test suite mocks every network call, so it is fast, deterministic and works offline. Two live harnesses back it up:

- `validation/accuracy.py` checks freshpin's answers against independent sources.
- `validation/corpus.py` audits popular open-source repositories.

[`evals/`](evals/) holds the `claude plugin eval` A/B suite. See [CONTRIBUTING.md](CONTRIBUTING.md) for adding a registry or an auditor.

## License

[MIT](LICENSE-MIT)
