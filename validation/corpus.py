"""Real-world corpus audit: run freshpin over popular open-source repositories.

Run:  uv run python validation/corpus.py --workdir /tmp/freshpin-corpus [--markdown out.md]

Shallow-clones each repository (read-only: files are parsed, nothing is executed) and runs
``audit_paths`` on it, recording crashes, parse errors, declarations found, statuses and
timing. A sample of findings is printed for manual precision review.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from freshpin.auditors import audit_paths
from freshpin.cache import Cache
from freshpin.registries import Resolver

REPOS = [
    # Python
    "fastapi/full-stack-fastapi-template",
    "fastapi/fastapi",
    "pallets/flask",
    "psf/requests",
    "encode/httpx",
    "pydantic/pydantic",
    "astral-sh/uv",
    # JavaScript / TypeScript
    "shadcn-ui/ui",
    "vitejs/vite",
    "expressjs/express",
    "sveltejs/kit",
    "tailwindlabs/tailwindcss",
    # Rust
    "BurntSushi/ripgrep",
    "tokio-rs/axum",
    "clap-rs/clap",
    # Docker / compose / mixed
    "docker/awesome-compose",
    "dockersamples/example-voting-app",
    "actions/starter-workflows",
]


def clone(repo: str, workdir: Path) -> Path:
    dest = workdir / repo.replace("/", "__")
    if not dest.exists():
        subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", f"https://github.com/{repo}", str(dest)],
            check=True,
            timeout=600,
        )
    return dest


def starter_workflows_as_workflows(root: Path) -> None:
    """starter-workflows keeps templates outside .github/workflows; expose them there."""
    target = root / ".github" / "workflows" / "_templates"
    if target.exists():
        return
    target.mkdir(parents=True)
    for folder in ("ci", "deployments", "automation", "code-scanning", "pages"):
        for yml in sorted((root / folder).glob("*.yml")):
            (target / f"{folder}__{yml.name}").write_text(yml.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--sample", type=int, default=60)
    args = parser.parse_args()
    args.workdir.mkdir(parents=True, exist_ok=True)

    with ThreadPoolExecutor(max_workers=6) as pool:
        paths = dict(
            zip(REPOS, pool.map(lambda r: _safe_clone(r, args.workdir), REPOS), strict=True)
        )
    if (p := paths.get("actions/starter-workflows")) is not None:
        starter_workflows_as_workflows(p)

    resolver = Resolver(cache=Cache(args.workdir / "cache.json", ttl=6 * 3600))
    rows = []
    all_findings = []
    started = time.time()
    for repo, path in paths.items():
        if path is None:
            rows.append({"repo": repo, "clone_failed": True})
            continue
        t0 = time.time()
        try:
            report = audit_paths([path], resolver, base=path)
        except Exception:  # a crash is a bug we want to see
            rows.append({"repo": repo, "crash": traceback.format_exc(limit=3)})
            continue
        counts = report.counts()
        kinds = Counter(f.ref.kind for f in report.findings)
        rows.append(
            {
                "repo": repo,
                "files": len(report.files),
                "declarations": len(report.findings),
                **counts,
                "parse_errors": report.errors,
                "kinds": dict(kinds),
                "seconds": round(time.time() - t0, 2),
            }
        )
        for f in report.findings:
            all_findings.append({"repo": repo, **f.to_dict(path)})
    elapsed = time.time() - started

    lines = render(rows, all_findings, elapsed, args.sample)
    print("\n".join(lines))
    if args.markdown:
        args.markdown.write_text("\n".join(lines) + "\n")
    if args.json:
        args.json.write_text(json.dumps({"rows": rows, "findings": all_findings}, indent=1))
    return 1 if any("crash" in r for r in rows) else 0


def _safe_clone(repo: str, workdir: Path) -> Path | None:
    try:
        return clone(repo, workdir)
    except Exception:
        return None


def render(
    rows: list[dict[str, object]], findings: list[dict[str, object]], elapsed: float, sample: int
) -> list[str]:
    out = [f"# freshpin real-world corpus audit ({time.strftime('%Y-%m-%d')})", ""]
    out += [
        "| Repository | Files | Declarations | Outdated | OK | Unpinned | Skipped | Errors | Parse errors | Seconds |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    totals: Counter[str] = Counter()
    for r in rows:
        if "crash" in r or "clone_failed" in r:
            label = "CRASH" if "crash" in r else "clone failed"
            out.append(f"| {r['repo']} | {label} | | | | | | | | |")
            continue
        perr = r["parse_errors"]
        assert isinstance(perr, list)
        out.append(
            f"| {r['repo']} | {r['files']} | {r['declarations']} | {r['outdated']} | {r['ok']} | "
            f"{r['unpinned']} | {r['skipped']} | {r['error']} | {len(perr)} | {r['seconds']} |"
        )
        for key in ("files", "declarations", "outdated", "ok", "unpinned", "skipped", "error"):
            value = r[key]
            assert isinstance(value, int)
            totals[key] += value
        totals["parse_errors"] += len(perr)
    out.append(
        f"| **Total** | {totals['files']} | {totals['declarations']} | {totals['outdated']} | "
        f"{totals['ok']} | {totals['unpinned']} | {totals['skipped']} | {totals['error']} | "
        f"{totals['parse_errors']} | {elapsed:.1f} |"
    )
    crashes = [r for r in rows if "crash" in r]
    out += ["", f"Crashes: {len(crashes)}"]
    for r in crashes:
        out += [f"```\n{r['repo']}\n{r['crash']}```"]
    perrs = [(r["repo"], e) for r in rows for e in r.get("parse_errors", []) or []]  # type: ignore[union-attr]
    out += ["", "## Parse errors", ""] + ([f"- `{repo}`: {e}" for repo, e in perrs] or ["None."])
    errs = [f for f in findings if f["status"] == "error"]
    out += ["", "## Lookup errors", ""] + (
        [f"- `{f['repo']}` {f['lookup']}: {f.get('note')}" for f in errs[:40]] or ["None."]
    )
    outdated = [f for f in findings if f["status"] == "outdated"]
    rng = random.Random(42)
    picked = rng.sample(outdated, min(sample, len(outdated)))
    out += [
        "",
        f"## Random sample of {len(picked)} outdated findings (seed 42) for manual review",
        "",
        "| Repo | Location | Current | Suggested | Bump | Note |",
        "|---|---|---|---|---|---|",
    ]
    for f in picked:
        out.append(
            f"| {f['repo']} | {f['file']}:{f['line']} | `{f['current']}` | `{f['suggested']}` | "
            f"{f['bump']} | {str(f.get('note') or '')[:80]} |"
        )
    return out


if __name__ == "__main__":
    raise SystemExit(main())
