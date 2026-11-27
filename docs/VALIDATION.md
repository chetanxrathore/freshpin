# Validation: does freshpin work?

All results below were measured on **2026-11-26** against freshpin 0.1.0, except the code-quality checks ([section 4](#4-code-quality)), which were re-run on **2026-11-27** against 0.2.0. They come from Claude Code 2.1.326 on macOS (Apple silicon). Every run can be reproduced with the commands shown.

| Question | Method | Result |
|---|---|---|
| Does it change what an AI agent writes? | A/B `claude plugin eval`: the same prompts with and without the plugin, deterministic graders | **With freshpin: 39/39 checks pass (100%). Without: 0/39.** The skill fired in 6/6 runs. |
| Are its answers correct? | Compared with independent sources of truth | **97/97 versions match; 43/43 suggested Docker tags and 22/22 suggested `uses:` refs exist** |
| Does it survive real projects? | Audited 18 popular open-source repositories | **12,135 declarations in 1,321 files: 0 crashes, 0 parse errors; 78/78 Docker suggestions verified** |
| Is the code sound? | Offline unit and integration tests, ruff, `mypy --strict`, every supported Python | **393 tests pass on Python 3.9–3.15; 95% branch coverage** |
| Is the plugin well formed? | `claude plugin validate . --strict` | **Passes with nothing to report** |

---

## 1. Agent impact: A/B eval in Claude Code

**Setup.** [`evals/`](../evals/) holds two scaffolding tasks:

- **FastAPI + Postgres + Redis + CI:** 7 graded versions.
- **Next.js + TypeScript + Docker + CI:** 6 graded versions.

`claude plugin eval` runs each prompt **with** the freshpin plugin (skill + PostToolUse hook) and **without** it (a baseline arm with no plugin). The prompts never mention freshpin or any version number. Both arms are told to "use current, production-appropriate versions", may run shell commands, and may reach PyPI, npm, GitHub, endoflife.date and Docker Hub. Graders are deterministic regexes over the files the agent wrote; no LLM judge is used.

```bash
claude plugin eval . --runs 3 -j 3 --model claude-sonnet-5-5 --keep-temp --max-cost-usd 8 \
  --allow-tools Bash "WebFetch(domain:pypi.org)" "WebFetch(domain:registry.npmjs.org)" \
  "WebFetch(domain:github.com)" "WebFetch(domain:api.github.com)" \
  "WebFetch(domain:endoflife.date)" "WebFetch(domain:hub.docker.com)"
```

**Final result** (model `claude-sonnet-5-5`, 3 runs per arm per case, 12 agent sessions, $1.10, 86 s):

| Case | With freshpin | Without | Δ |
|---|---|---|---|
| fastapi-stack | 7/7, 7/7, 7/7 (8 turns each) | 0/7, 0/7, 0/7 | **+100 points** |
| nextjs-app | 6/6, 6/6, 6/6 (7–8 turns) | 0/6, 0/6, 0/6 | **+100 points** |

**What each arm actually wrote** (final file contents, counted across runs):

| Declaration | Without freshpin (from memory) | With freshpin |
|---|---|---|
| Python base image | `python:3.13` ×2, `python:3.12` ×1 | `python:3.15` ×3 |
| PostgreSQL image | `postgres:17` ×3 | `postgres:19` ×3 |
| Redis image | `redis:7` ×3 | `redis:8.12` ×3 |
| Node.js image | `node:24` ×3 | `node:26` (current LTS) ×3 |
| `actions/checkout` | `@v5` ×4, `@v4` ×2 | `@v7` ×6 |
| `actions/setup-python` | `@v5` ×2, `@v6` ×1 | `@v7` ×3 |
| `actions/setup-node` | `@v5` ×3 | `@v7` ×3 |
| CI Python / Node version | 3.13 / 3.12; 24 | 3.15; 26 |
| `fastapi` | `>=0.115.0` ×3 | `>=0.165.0` ×3 |
| `next` / `react` / `typescript` | `^16.0.0` / `^19.2.0` / `^5.9.0` | `^16.4.6` / `^19.3.1` / `^7.0.2` |

The baseline is not careless. It even queried PyPI and npm with `curl` in some runs. It still landed on the versions in its training data for images, Actions and runtimes, which is the failure the research describes.

### Eval-driven fixes

The eval was run four times. The intermediate rounds exposed three real-world bugs that unit tests could not:

| Round | Code | With | Without | What it revealed |
|---|---|---|---|---|
| 1 | initial | 26/26 | 0/26 | (2 runs per arm) |
| 2 | initial | 9/13 | 3/13 | (1 run per arm) Inside Claude Code's sandbox only macOS's built-in **Python 3.9** could start, so the skill's launcher (then 3.10+) refused to run. The agent also wrote files with **Bash heredocs**, which a Write/Edit-only hook never sees. |
| 3 | + Python 3.9 support, + Bash hook | 39/39 | 0/39 | The first `python3` on PATH can be an interpreter the sandbox blocks. |
| 4 (final) | + interpreter-picking `sh` launchers | 39/39 | 0/39 | — |

The fixes:

1. The package now supports Python 3.9+, and the suite passes on 3.9.6.
2. The hook also runs on `Bash`. It audits files written by `cat > f <<EOF`, `tee` and redirects, plus packages added by `npm/pnpm/yarn/bun install|add`, `uv/poetry/pdm add` and `cargo add`.
3. `skills/freshpin/scripts/freshpin` and `hooks/freshpin-hook.sh` pick the first Python ≥ 3.9 that actually starts, even with a minimal PATH.

Total eval spend across all four rounds: $3.45.

---

## 2. Accuracy against independent sources

[`validation/accuracy.py`](../validation/accuracy.py) asks freshpin and a **different authority** the same question:

| Ecosystem | freshpin reads | Independent source | Checked | Agree |
|---|---|---|---|---|
| PyPI | `/pypi/<pkg>/json` | PEP 691 simple index + reference `packaging` library | 25 | 25 |
| npm | `/<pkg>/latest` (+ dist-tags when `latest` is a pre-release) | `npm view <pkg> dist-tags` | 25 | 25 |
| crates.io | API `max_stable_version` | sparse index `index.crates.io` | 20 | 20 |
| GitHub Actions | `releases/latest` redirect + git refs | `git ls-remote --tags`, highest semver | 22 | 22 |
| Runtimes | endoflife.date (LTS-aware) | python.org, nodejs.org (LTS), go.dev, static.rust-lang.org, postgresql.org | 5 | 5 |
| Docker | suggested tags | Docker Hub tag API returns 200 | 43 | 43 |
| `uses:` refs | suggested ref (`@v7`, or the full tag) | `git ls-remote` lists it as a tag or branch | 22 | 22 |

```bash
uv run --with packaging python validation/accuracy.py --markdown validation/results/accuracy.md
```

The **first** run of this harness found three defects that the fixture tests could not:

- **GitHub lookups:** all 20 failed. The anonymous REST API allows 60 requests an hour per IP, and that quota was used up.
  - Fixed with token-free GitHub resolution: a `releases/latest` redirect plus the git smart-HTTP ref list.
  - The final run passed 20/20 **while the API quota was at zero**.
- **Docker tags:** freshpin suggested `elasticsearch:9.5`, a tag that does not exist on Docker Hub.
  - Fixed: suggested tags are verified on Docker Hub, with a fallback to the full version (`9.5.7`).
- **npm pre-releases:** `latest` pointed at a release candidate (`prisma 8.0.0-rc.35`).
  - Fixed with stable-only resolution through npm dist-tags (`7.10.1`).

**Writing freshpin's own release workflow exposed a fourth defect.** freshpin suggested a floating major tag (`@v10`) for every action, but not every action publishes one:

- astral-sh/setup-uv stopped publishing them after `v7`.
- pypa/gh-action-pypi-publish has only full tags and `release/v1` branches.
- ruby/setup-ruby publishes `v1` as a branch.

So 2 of the 22 suggested refs did not exist (`astral-sh/setup-uv@v10` and `pypa/gh-action-pypi-publish@v1`), and a workflow that used them would fail before any step ran. For the same reason, an agent's habitual `@v1` was reported as "ok".

- Fixed: freshpin now suggests the least precise tag or branch that exists (`astral-sh/setup-uv@v10.4.0`). A floating ref that is neither a tag nor a branch is reported as outdated at every `--level`.
- The accuracy check now verifies each suggested ref with `git ls-remote`: 22/22, up from 20/22 with the old logic.

Full per-package results: [`validation/results/accuracy.md`](../validation/results/accuracy.md).

---

## 3. Real-world corpus

[`validation/corpus.py`](../validation/corpus.py) shallow-clones and audits 18 popular repositories. Their files are only parsed, never executed. The repositories:

- **Python:** full-stack-fastapi-template, fastapi, flask, requests, httpx, pydantic, uv.
- **JavaScript/TypeScript:** shadcn/ui, vite, express, SvelteKit, tailwindcss.
- **Rust:** ripgrep, axum, clap.
- **Docker / CI templates:** awesome-compose, example-voting-app, GitHub's starter-workflows.

| Metric | Result |
|---|---|
| Files / declarations | 1,321 / 12,135 |
| Crashes / parse errors | **0 / 0** |
| Audit time (warm cache) | 3.2 s for all 18 repositories |
| Outdated / OK / unpinned / skipped | 3,708 / 3,845 / 1,061 / 3,378 |
| Lookup errors | 143: 141 are private or test-only PyPI names in uv's test fixtures (`pyx-member-*`) that are not on PyPI |
| Docker suggestions verified on Docker Hub | **78/78** (62/78 before the OS-variant repair) |

**Precision review.** I manually reviewed a seeded random sample of 60 "outdated" findings. All 60 were factually correct: a newer stable release exists. The review found four problems with the *suggestions*, all since fixed:

1. Exact pins with post-releases: `==4.3.post1` was suggested as `==4.5`, which may not exist. Exact pins now get the full latest version, `==4.5.post3`.
2. Compound ranges dropped a cap that still admits the latest release: `click>=7.0,<9.0.0` became `>=8.5.1`. Now `>=8.5,<9.0.0`.
3. A deliberate single-line runtime pin was widened: `requires-python = "==3.14.*"` became `>=3.11`. Now `==3.15.*`.
4. Retired OS variants produced unpublished tags, such as `python:3.15-buster` and `mariadb:12-focal`. These are now modernised to current Debian, Ubuntu LTS or Alpine releases and verified: `python:3.15-trixie`, `mariadb:12-noble`, `node:26.13.1-alpine3.24`.

**Policy note.** 475 of the 3,708 outdated findings (13%) are "stale floor" findings. These are ranges such as `packaging>=20` that still allow the latest release but whose lower bound is a breaking release behind. That is correct for an agent writing a *new* project, but it can be noisy for libraries. `--no-floor-check` reports only constraints that exclude the latest release.

Full table and the reviewed sample: [`validation/results/corpus.md`](../validation/results/corpus.md).

---

## 4. Code quality

```bash
uv run ruff check src tests hooks skills validation
```

```bash
uv run mypy --strict src/freshpin
```

```bash
uv run pytest -v
```

| Check | Result |
|---|---|
| ruff (with security rules) | clean |
| `mypy --strict` (package, hook, launcher) | clean, 23 files |
| pytest | 393 passed: versioning 74, cache 15, registries 76, auditors 103, missing and yanked pins 74, CLI 23, hook 28. All offline; the network is blocked in tests. |
| Branch coverage | 95% (`coverage run --branch --source=src/freshpin -m pytest`) |
| Python 3.9.6 / 3.10.20 / 3.11.6 / 3.12.13 / 3.13.5 / 3.14.5 / 3.15.0 | 393/393 on each |
| Wheel / sdist | build; `twine check --strict` passes; zero runtime dependencies; `Requires-Python: >=3.9` |
| `claude plugin validate . --strict` | passes, nothing to report |

## 5. Plugin behaviour (installed-copy simulation)

The repository was copied to a separate folder, as Claude Code installs plugins, and the exact `hooks.json` command was run with an interpreter that does **not** have freshpin installed:

| Scenario | Result |
|---|---|
| Write a stale Dockerfile | `decision: block` with `python:3.10-slim -> python:3.15-slim`, `postgres:14-alpine -> postgres:19-alpine` |
| Edit one line | only the edited line is reported |
| Relative path, up-to-date file, non-manifest file | correct; non-manifest files return in about 50 ms |
| Context mode / `FRESHPIN_HOOK=off` / malformed input / offline | all behave as designed; the hook always exits 0 |
| Skill launcher on macOS `/usr/bin/python3` (3.9.6) | resolves live |

## Limits of this validation

- The A/B eval covers **one model** (`claude-sonnet-5-5`) and two scaffolding tasks, with 3 runs per arm. Graders encode the versions that were current on 2026-11-26 and must be refreshed (see `evals/README.md`).
- Accuracy checks a fixed list of popular packages, not the long tail.
- The corpus "outdated" counts are freshpin's own policy applied to real repositories. Correctness was checked on a 60-finding sample, not on all 3,708.
