# freshpin evolution plan

This plan follows from the landscape research ([docs/RESEARCH.md](docs/RESEARCH.md)) and the validation runs ([docs/VALIDATION.md](docs/VALIDATION.md)). It was written on 2026-11-26 against freshpin 0.1.0.

## Where freshpin stands

**The problem is measured, not hypothetical.**

- A May 2026 study of 10 frontier models found the median Python dependency version they chose was 9–31 months older than the model's training cutoff. 36.7–55.7% of the chosen versions had known CVEs, and 11.2% did not exist at all.
- In the same study, giving the model live, resolved versions cut CVE exposure from about 40% to about 10–12%. Telling the model in words to avoid vulnerable versions did nothing. That is exactly the mechanism freshpin automates.
- Industry data points the same way:
  - Datadog 2026: the median dependency is 278 days behind its latest major.
  - Sonatype 2026: GPT-5 invented versions in 27.8% of upgrade recommendations.

**Most tools that already exist leave a gap.** Each group misses the moment an AI agent writes a file:

- **Bots** (Renovate, Dependabot) act after the commit, through pull requests, and now wait 3 days by default.
- **CLIs** (npm-check-updates, taze, pur, `uv tree --outdated`, cargo-outdated) cover one ecosystem each and are not in the agent loop.
- **MCP servers** (package-version-check-mcp, mcp-devtools, Context7 for docs) only help if the model decides to call them. Their own READMEs admit that models often don't.
- **Security MCPs** (Snyk, Socket, Sonatype, Endor Labs) answer "is this version safe or allowed?", not "what is current?". They need accounts and tokens.

**The closest projects** are small:

- yul: a research prototype using a PreToolUse hook, with no Docker or EOL coverage.
- safer-dependencies: source-available, not open source.
- agent-versions: no GitHub Actions or Docker coverage.
- pinact and actions-up: excellent, but GitHub Actions only.

**freshpin's differentiators** are confirmed by the research:

1. One tool covers packages, GitHub Actions, Docker base images and end-of-life runtimes or databases.
2. It enforces in the agent's loop: a PostToolUse hook reports only the lines that were just written.
3. It needs no token or account, and it has zero dependencies.
4. Its suggestions are verified, not guessed:
   - GitHub tags are read from git refs, with the real commit SHA for pinned actions.
   - Docker tags are checked on Docker Hub, and retired OS variants are modernised.
5. It picks LTS releases for runtimes that have them.

## Guiding principles

- **Live data beats instructions.** Every feature should hand the agent resolved facts rather than advice.
- **"Latest" must also be safe and installable.** Never recommend a version that is too new, vulnerable, yanked, deprecated or incompatible with the project.
- **Enforce at write time, verify in CI.** Hooks give fast feedback, and `audit --fail-on-outdated` is the backstop.
- **Stay small:** stdlib-only, no token required, and every new data source has to degrade gracefully when offline.

---

## v0.2: "Latest, but safe" (next release)

The goal is to answer the strongest objection from security vendors and classic bots: the newest version is not always the safest.

| # | Feature | Why (evidence) | Notes |
|---|---|---|---|
| 1 | **Release-age cooldown** (`--min-age 3d`, default 3 days for hooks) | Dependabot (3 days by default since 2026-07-14), Renovate presets, npm/pnpm/uv/Poetry/pip all added cooldowns in 2025–26. 8 of 10 studied supply-chain attacks were exploited within a week. | Needs release timestamps: PyPI `upload_time`, npm `time`, crates `created_at`, GitHub `published_at`. Recommend the newest version older than the cooldown. |
| 2 | **Vulnerability-aware latest** via [OSV](https://osv.dev) (`--safe`) | 36.7–55.7% of model-chosen versions carried CVEs. | One batched OSV query per audit. Flag pinned versions with known vulnerabilities even when they are current. |
| 3 | **Non-existent and yanked version detection** *(shipped)* | 11.2% of model-written versions don't exist on PyPI. Endor Labs reports 34%. | Exact pins (`==`/`===`, npm and Poetry exact versions, Cargo `=`) are checked against the registry's published versions. Two new statuses, `missing` and `yanked`, are reported at every `--level`, listed by `--format agent` and the hook, and fail `--fail-on-outdated`. `yanked` covers PyPI and crates.io yanks, and npm versions that are `deprecated` while the latest release is not. PyPI and crates.io reuse the package response, so there is no extra request. npm costs one cached per-version lookup per exact pin that isn't the latest release. A pin missing from a cached list is re-checked live before it is reported. The suggested fix stays on the pin's release line (same major, or same minor on `0.x`) when that line has another release. Pins that may install from a private index or source (requirements index options, uv sources and indexes, Poetry sources) are not checked. A GitHub Action ref that doesn't exist now reports `missing` too. |
| 4 | **Project-compatible "latest"** | `uv tree --outdated` picks fastapi 0.162.5 rather than 0.165.0 because 0.162.6+ requires Python 3.11+. | Filter candidates by `requires_python`, npm `engines.node` and Cargo `rust-version` before recommending. |
| 5 | **Audit Bash install commands** *(partly shipped: shell file writes and manifest-editing `install`/`add`; remaining: manifest-less `pip install x==`, `npx`/`uvx`)* | yul's benchmark missed `npm install x@1` run through Bash. A January 2026 incident shows hallucinated `npx` commands spreading to 237+ repos. | Add a PostToolUse `Bash` matcher that parses `pip/uv/poetry add`, `npm/pnpm/yarn add`, `cargo add`, `go get` and `docker pull`. |
| 6a | **Tiered hook severity** | pinright measured 5.9–7.5 agent turns per task with a blocking hook vs 4.0–4.3 without, and Version Sentinel backed away from blocking. | Block only on EOL, missing, vulnerable or constraint-excluded versions. Add plain context for routine major bumps. Offer "latest within current major". |
| 6 | **GitHub Actions lockfile and pin-style awareness** | GitHub workflow lockfiles (`actions.lock`) became generally available on 2026-10-28, SHA pinning is now enforceable by policy, and zizmor defaults to SHA pins. | Detect the repo's pin style and suggest the same style. When `actions.lock` exists, tell the agent to relock. |

## v0.3: "Every agent, every surface"

| # | Feature | Notes |
|---|---|---|
| 7 | **MCP server mode** (`freshpin mcp`) | A stdlib JSON-RPC stdio server with `resolve`, `audit_file` and `check_version` tools. It makes freshpin usable from Cursor, Copilot, Codex, Gemini CLI, Windsurf, Cline and Junie with no hooks. |
| 8 | **Hook adapters** for Codex (`apply_patch` with `Edit`/`Write` aliases), Gemini CLI extensions (hooks and skills bundled since v0.26) and Copilot CLI (reads `.claude/settings.json`, PreToolUse only) | Same audit core, different payload shapes. Cursor's `postToolUse` context injection was reported broken in the forums, so verify it before claiming support. |
| 9 | **`freshpin install --agent <name>`** | Copies the skill to the right place: `.claude/skills`, `.agents/skills` (Codex, Gemini and Cursor aliases), `.github/skills` (Copilot), `.gemini/skills`, `.junie/skills`. Also writes a matching `AGENTS.md` / `.cursor/rules` snippet for agents with weak hooks. |
| 10 | **GitHub Action + SARIF output** (`--format sarif`) | Findings appear as code-scanning alerts on pull requests. pinact already offers SARIF. |
| 11 | **pre-commit hook** (`.pre-commit-hooks.yaml`) | `freshpin audit --fail-on-outdated` on changed files. |
| 11a | **Session-start summary and subagent coverage** | A `SessionStart` hook summarises what is already outdated, and hooks also cover files written by subagents. safer-dependencies and agent-versions already do both. |
| 11b | **Scan agent instructions for install commands** | Flag `npx`/`uvx`/`pip install` lines in `SKILL.md`, `AGENTS.md` and `CLAUDE.md` that name missing or stale packages. The react-codeshift incident spread through skill files. |

## v0.4: "More of the stack"

| # | Feature | Source |
|---|---|---|
| 12 | Go modules (`go.mod`) | proxy.golang.org (`@latest`, retracted and deprecated info) |
| 13 | Maven / Gradle, RubyGems, NuGet, Packagist, Hex, pub.dev | Registry JSON APIs. deps.dev v3 covers several with one schema. |
| 14 | Terraform/OpenTofu providers and modules, Helm charts | registry.terraform.io, Artifact Hub |
| 15 | Version files: `.nvmrc`, `.python-version`, `rust-toolchain.toml`, `.tool-versions` / mise, `runs-on: ubuntu-22.04` runner labels | endoflife.date plus GitHub runner images |
| 16 | Any container image (not only endoflife.date products), including ghcr.io and quay.io | OCI distribution API tag listing (`/v2/<repo>/tags/list`), so arbitrary images get "newest tag with the same suffix" |
| 17 | Kubernetes manifests and `pre-commit` `rev:` fields | Same image and GitHub resolvers |
| 18 | endoflife.date **API v1** | 490 products. Use `isLts`/`isEol` and its `pkg:docker/...` identifiers to map images to products automatically instead of keeping a hand-written table. |

## v0.5: "Fix, don't just flag"

| # | Feature | Notes |
|---|---|---|
| 19 | **`freshpin audit --fix`** | Apply suggestions in place while preserving formatting and comments. The `template` model already knows each declaration's exact text. |
| 20 | `--diff` and interactive mode | Review before writing. |
| 21 | **Config file** (`[tool.freshpin]` in pyproject or `.freshpin.toml`) | Ignore lists, level, cooldown, per-ecosystem policy (strict / library / LTS-only), excluded paths. |
| 22 | **Respect existing bot config** | Read `renovate.json` / `dependabot.yml` ignore rules, cooldowns and allowed versions so freshpin never fights the repo's bot. |
| 23 | Monorepo awareness | Workspaces (npm/pnpm/yarn, Cargo, uv), catalog versions, `workspace:` references. |

## v1.0: "Proven and trusted"

| # | Feature | Notes |
|---|---|---|
| 24 | **Published benchmark** | Run the `evals/` A/B suite across several models every release and publish the score deltas. This would be the first public measurement for GitHub Actions and Docker staleness; no study measures those today. |
| 25 | Nightly live-accuracy CI | `validation/accuracy.py` against independent sources. Fail on any mismatch or unpublished Docker tag. |
| 26 | Release provenance | PyPI Trusted Publishing with PEP 740 and GitHub build-provenance attestations is in place (`.github/workflows/release.yml`). Still to do: signed tags and an SBOM. |
| 27 | Docs site and listing in the Claude Code plugin directory | Use the icon and validator-clean manifest already in place. |
| 28 | Caching upgrades | ETags / `If-None-Match` and a shared, opt-in team cache. Strictly no telemetry. |

## Non-goals

These areas already have strong owners. freshpin should integrate with them rather than rebuild them:

- **Pinning and lockfiles:** GitHub `actions.lock` and pinact. freshpin should *respect* them (item 6), not reimplement them.
- **Documentation retrieval:** Context7 and others. Pairing freshpin with them is the better story.
- **Security scoring:** Socket, Snyk, Sonatype, Endor. freshpin uses open OSV data only to avoid recommending known-bad versions.
- **Renovate's code:** it is AGPL, so any cross-check (e.g. a `--via renovate` mode) must run Renovate as a separate process.

---

## Known limitations today (v0.1)

- **The newest stable version is recommended without a release-age cooldown or vulnerability check.** Planned for v0.2, items 1 and 2.
- **Stale floors on libraries:** a lower bound a breaking release behind (`packaging>=20`) is reported by default. Libraries should use `--no-floor-check`.
- **Docker coverage:** only images that map to an endoflife.date product are evaluated (about 60 images). Other images are listed as skipped.
- **Registry coverage:** suggested tags are verified to exist on Docker Hub, and suggested GitHub Action refs against the repository's tags and branches. Exact package pins are checked against PyPI, npm and crates.io (`missing`, `yanked`). Not yet checked:
  - ranges whose floor names a version that doesn't exist (`fastapi>=0.999`, `"next": "^99.0.0"`). These are reported as `ok` today, because a floor above the latest release is never "behind".
  - package *names* that don't exist. These are reported as `error` ("not found on pypi"), not `missing`, so they don't fail `--fail-on-outdated`.
  - exact Docker tags that aren't endoflife.date products.
- **Offline existence checks:** a pin missing from a cached version list is not reported as `missing` until freshpin can confirm it live. Until then it is listed as `unverified` in agent output and passed to the hook as non-blocking context.
- **Private indexes configured outside the file are not seen:** pip, uv and Poetry config files, `PIP_INDEX_URL`-style environment variables, index options in a `-r` file, and npm registries in `.npmrc`. Pins installed from such an index can be reported as `missing` when PyPI or npm has a package of the same name.
- **npm fixes for missing or deprecated pins:** npm has no cheap version list, so the suggestion is the latest release, even across a major version. The note then names the `npm view` command for the newest release on the pin's line.
- **GitHub releases:** "latest" follows the repository's own *latest release* marker. If a repository never marks one, the highest semver tag is used.
- **Hook coverage:** `Write`/`Edit`/`MultiEdit` and `Bash` are audited. Bash covers file writes, plus `install`/`add` commands that edit a manifest. Not yet covered:
  - `pip install x==1.0` with no manifest
  - `npx`/`uvx` one-off runs
  - files written by subagents (item 11a)
- **Release model changes:** the A/B eval graders and the hard-coded LTS assumptions must be refreshed when release models change. For example, Node.js 27+ makes every major an LTS. The LTS check reads endoflife.date's dates, so the logic itself adapts; the test data does not.
