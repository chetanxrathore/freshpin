# freshpin agent evals

A/B evals for `claude plugin eval`: the same prompt runs **with** the freshpin plugin
(skill + PostToolUse hook) and **without** it (baseline arm), and deterministic `regex`
graders check whether the files Claude wrote use current versions. The prompts never
mention freshpin or any version number, so the baseline is a fair "what the model
writes from memory" measurement.

Run (from the repo root; needs a signed-in `claude`):

```bash
claude plugin eval . --runs 2 --model claude-sonnet-5-5 --allow-tools Bash \
  "WebFetch(domain:pypi.org)" "WebFetch(domain:registry.npmjs.org)" \
  "WebFetch(domain:github.com)" "WebFetch(domain:endoflife.date)" \
  "WebFetch(domain:hub.docker.com)" --max-cost-usd 15
```

**Keep graders current.** They encode the latest stable versions on the day they were
written (2026-11-26). Refresh them with `freshpin resolve` before each run, e.g.
`freshpin resolve docker:python docker:postgres gha:actions/checkout npm:next -f agent`.
Results land in `evals/results/` (git-ignored).

## Latest results (2026-11-26, `claude-sonnet-5-5`, 3 runs per arm)

| Case | With freshpin | Without |
|---|---|---|
| fastapi-stack | 21/21 checks | 0/21 |
| nextjs-app | 18/18 checks | 0/18 |

The skill fired in 6/6 runs. Cost: $1.10. See [docs/VALIDATION.md](../docs/VALIDATION.md) for what each arm wrote.
