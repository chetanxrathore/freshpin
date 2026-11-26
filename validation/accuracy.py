"""Live accuracy check: compare freshpin's answers with independent sources of truth.

Run:  uv run --with packaging python validation/accuracy.py [--markdown out.md]

For every ecosystem freshpin answers from one endpoint; this script asks a *different*
authority and reports agreement:

  PyPI        freshpin: /pypi/<pkg>/json          truth: PEP 691 simple index + `packaging`
  npm         freshpin: registry /<pkg>/latest    truth: `npm view <pkg> dist-tags.latest`
  crates.io   freshpin: API max_stable_version    truth: sparse index (index.crates.io)
  GitHub      freshpin: releases/latest (+tags)   truth: `git ls-remote --tags` max semver
  runtimes    freshpin: endoflife.date            truth: python.org, nodejs.org, go.dev,
                                                         static.rust-lang.org, postgresql.org
  Docker      freshpin's suggested tags           truth: Docker Hub tag exists (HTTP 200)

Needs network access; makes ~150 HTTP requests.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from freshpin.cache import Cache
from freshpin.registries import Resolver
from freshpin.registries.base import USER_AGENT

PYPI = [
    "requests",
    "fastapi",
    "pydantic",
    "django",
    "flask",
    "numpy",
    "pandas",
    "sqlalchemy",
    "httpx",
    "uvicorn",
    "pytest",
    "ruff",
    "mypy",
    "boto3",
    "celery",
    "redis",
    "langchain",
    "openai",
    "anthropic",
    "torch",
    "scikit-learn",
    "black",
    "poetry",
    "typer",
    "rich",
]
NPM = [
    "react",
    "react-dom",
    "next",
    "vue",
    "svelte",
    "@angular/core",
    "typescript",
    "vite",
    "express",
    "@types/node",
    "eslint",
    "prettier",
    "tailwindcss",
    "zod",
    "axios",
    "lodash",
    "jest",
    "vitest",
    "@tanstack/react-query",
    "prisma",
    "webpack",
    "esbuild",
    "astro",
    "@sveltejs/kit",
    "nuxt",
]
CRATES = [
    "serde",
    "tokio",
    "reqwest",
    "clap",
    "anyhow",
    "thiserror",
    "rand",
    "regex",
    "axum",
    "serde_json",
    "tracing",
    "chrono",
    "hyper",
    "sqlx",
    "rayon",
    "itertools",
    "uuid",
    "log",
    "futures",
    "bytes",
]
ACTIONS = [
    "actions/checkout",
    "actions/setup-python",
    "actions/setup-node",
    "actions/cache",
    "actions/upload-artifact",
    "actions/download-artifact",
    "actions/setup-go",
    "actions/setup-java",
    "docker/build-push-action",
    "docker/login-action",
    "docker/setup-buildx-action",
    "astral-sh/setup-uv",
    "github/codeql-action",
    "pypa/gh-action-pypi-publish",
    "softprops/action-gh-release",
    "peaceiris/actions-gh-pages",
    "codecov/codecov-action",
    "dtolnay/rust-toolchain",
    "pnpm/action-setup",
    "aws-actions/configure-aws-credentials",
]
# Docker images and the tag variants people actually write.
DOCKER = {
    "python": ["{cycle}", "{cycle}-slim", "{cycle}-alpine"],
    "node": ["{cycle}", "{cycle}-alpine", "{cycle}-slim"],
    "postgres": ["{cycle}", "{cycle}-alpine"],
    "mysql": ["{cycle}"],
    "mariadb": ["{cycle}"],
    "redis": ["{cycle}", "{cycle}-alpine"],
    "mongo": ["{cycle}"],
    "golang": ["{cycle}", "{cycle}-alpine"],
    "rust": ["{cycle}", "{cycle}-slim"],
    "ruby": ["{cycle}", "{cycle}-slim"],
    "php": ["{cycle}", "{cycle}-fpm"],
    "ubuntu": ["{cycle}"],
    "debian": ["{cycle}", "{cycle}-slim"],
    "alpine": ["{cycle}"],
    "nginx": ["{cycle}", "{cycle}-alpine"],
    "httpd": ["{cycle}"],
    "rabbitmq": ["{cycle}", "{cycle}-management"],
    "elasticsearch": ["{cycle}"],
    "eclipse-temurin": ["{cycle}", "{cycle}-jdk"],
    "haproxy": ["{cycle}"],
    "traefik": ["v{cycle}"],
    "caddy": ["{cycle}"],
    "tomcat": ["{cycle}"],
    "memcached": ["{cycle}"],
    "erlang": ["{cycle}"],
    "elixir": ["{cycle}"],
    "perl": ["{cycle}"],
    "fedora": ["{cycle}"],
    "amazoncorretto": ["{cycle}"],
}


def get(url: str, accept: str = "application/json") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return bytes(resp.read())


def stable_max(versions: list[str]) -> str | None:
    best: tuple[Version, str] | None = None
    for raw in versions:
        try:
            v = Version(raw.lstrip("vV"))
        except InvalidVersion:
            continue
        if v.is_prerelease or v.is_devrelease:
            continue
        if best is None or v > best[0]:
            best = (v, raw)
    return best[1] if best else None


# --- independent sources of truth -------------------------------------------------------


def truth_pypi(name: str) -> str | None:
    data = json.loads(
        get(f"https://pypi.org/simple/{name}/", "application/vnd.pypi.simple.v1+json")
    )
    versions: dict[str, bool] = {}
    for f in data["files"]:
        m = re.match(
            rf"^{re.escape(data['name']).replace('-', '[-_.]')}-([^-]+?)(?:-|\.tar\.gz$|\.zip$)",
            f["filename"],
            re.IGNORECASE,
        )
        if m:
            ok = not f.get("yanked")
            versions[m.group(1)] = versions.get(m.group(1), False) or ok
    if data.get("versions"):  # PEP 700 adds an explicit versions list
        return stable_max([v for v in data["versions"] if versions.get(v, True)])
    return stable_max([v for v, ok in versions.items() if ok])


def truth_npm(name: str) -> str | None:
    """`npm view` dist-tags; if `latest` is a pre-release, the newest stable tag (stable policy)."""
    out = subprocess.run(
        ["npm", "view", name, "dist-tags", "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    tags = json.loads(out.stdout or "{}")
    latest = tags.get("latest")
    try:
        if latest and not Version(latest).is_prerelease:
            return str(latest)
    except InvalidVersion:
        pass
    return stable_max([v for v in tags.values() if isinstance(v, str)])


def truth_crates(name: str) -> str | None:
    n = name.lower()
    prefix = {1: "1", 2: "2", 3: f"3/{n[0]}"}.get(len(n), f"{n[:2]}/{n[2:4]}")
    lines = get(f"https://index.crates.io/{prefix}/{n}").decode().splitlines()
    entries = [json.loads(line) for line in lines if line.strip()]
    return stable_max([e["vers"] for e in entries if not e.get("yanked")])


def truth_github(repo: str) -> str | None:
    out = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", f"https://github.com/{repo}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    tags = [
        line.split("refs/tags/", 1)[1] for line in out.stdout.splitlines() if "refs/tags/" in line
    ]
    return stable_max(tags)


def truth_python() -> str | None:
    data = json.loads(
        get("https://www.python.org/api/v2/downloads/release/?is_published=true&pre_release=false")
    )
    return stable_max([r["name"].removeprefix("Python ") for r in data])


def truth_node_lts() -> str | None:
    data = json.loads(get("https://nodejs.org/dist/index.json"))
    lts = [r["version"].lstrip("v") for r in data if r.get("lts")]
    return stable_max(lts)


def truth_go() -> str | None:
    data = json.loads(get("https://go.dev/dl/?mode=json"))
    return stable_max([r["version"].removeprefix("go") for r in data if r.get("stable")])


def truth_rust() -> str | None:
    text = get("https://static.rust-lang.org/dist/channel-rust-stable.toml").decode()
    m = re.search(r'\[pkg\.rust\]\s*version = "(\d+\.\d+\.\d+)', text)
    return m.group(1) if m else None


def truth_postgres() -> str | None:
    data = json.loads(get("https://www.postgresql.org/versions.json"))
    rel = [f"{d['major']}.{d['latestMinor']}" for d in data if d.get("supported", True)]
    return stable_max(rel)


def docker_tag_exists(image: str, tag: str) -> bool:
    repo = image if "/" in image else f"library/{image}"
    ns, name = repo.split("/", 1)
    url = f"https://hub.docker.com/v2/namespaces/{ns}/repositories/{name}/tags/{tag}"
    try:
        get(url)
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


# --- comparison --------------------------------------------------------------------------


@dataclass
class Row:
    ecosystem: str
    name: str
    freshpin: str | None
    truth: str | None
    note: str = ""

    @property
    def verdict(self) -> str:
        if self.freshpin is None or self.truth is None:
            return "n/a"
        try:
            same = Version(self.freshpin.lstrip("vV")) == Version(self.truth.lstrip("vV"))
        except InvalidVersion:
            same = self.freshpin == self.truth
        return "match" if same else "MISMATCH"


def compare(
    ecosystem: str, names: list[str], truth: Callable[[str], str | None], resolver: Resolver
) -> list[Row]:
    results = resolver.resolve_many([(ecosystem, n) for n in names])
    with ThreadPoolExecutor(max_workers=8) as pool:
        truths = list(pool.map(lambda n: _safe(truth, n), names))
    rows = []
    for name, (t, err) in zip(names, truths, strict=True):
        r = results[(ecosystem, name)]
        rows.append(Row(ecosystem, name, r.version, t, note=err or (r.error or "")))
    return rows


def _safe(fn: Callable[[str], str | None], arg: str) -> tuple[str | None, str]:
    try:
        return fn(arg), ""
    except Exception as exc:  # report, don't crash the run
        return None, f"truth lookup failed: {type(exc).__name__}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--markdown", type=Path, help="also write a markdown report here")
    args = parser.parse_args()
    started = time.time()
    resolver = Resolver(cache=Cache(enabled=False))
    rows: list[Row] = []
    rows += compare("pypi", PYPI, truth_pypi, resolver)
    rows += compare("npm", NPM, truth_npm, resolver)
    rows += compare("crates", CRATES, truth_crates, resolver)
    rows += compare("gha", ACTIONS, truth_github, resolver)
    runtime_truth = {
        "python": truth_python,
        "nodejs": truth_node_lts,
        "go": truth_go,
        "rust": truth_rust,
        "postgresql": truth_postgres,
    }
    for product, fn in runtime_truth.items():
        res = resolver.resolve("eol", product)
        t, err = _safe(lambda _p, f=fn: f(), product)
        rows.append(Row("eol", product, res.version, t, note=err or (res.error or "")))

    # Docker: does every tag freshpin would suggest actually exist?
    from freshpin.registries.endoflife import docker_product

    docker_rows: list[tuple[str, str, bool | None]] = []
    from freshpin.cli import verify_docker_hints

    keys = [("eol", docker_product(i) or i) for i in DOCKER]
    resolved = resolver.resolve_many(keys)
    eol = dict(zip(resolved, verify_docker_hints(list(resolved.values()), resolver), strict=True))
    checks: list[tuple[str, str]] = []
    for image, variants in DOCKER.items():
        res = eol[("eol", docker_product(image) or image)]
        cycle = str(res.details.get("docker_tag") or res.details.get("cycle") or "")
        if not cycle:
            docker_rows.append((image, "(no cycle)", None))
            continue
        checks += [(image, v.format(cycle=cycle)) for v in variants]
    with ThreadPoolExecutor(max_workers=6) as pool:
        exists = list(pool.map(lambda c: _exists(*c), checks))
    docker_rows += [(img, tag, ok) for (img, tag), ok in zip(checks, exists, strict=True)]

    lines = report(rows, docker_rows, time.time() - started)
    print("\n".join(lines))
    if args.markdown:
        args.markdown.write_text("\n".join(lines) + "\n")
    mismatches = sum(r.verdict == "MISMATCH" for r in rows)
    missing = sum(ok is False for _, _, ok in docker_rows)
    return 1 if (mismatches or missing) else 0


def _exists(image: str, tag: str) -> bool | None:
    try:
        return docker_tag_exists(image, tag)
    except Exception:
        return None


def report(
    rows: list[Row], docker_rows: list[tuple[str, str, bool | None]], elapsed: float
) -> list[str]:
    out = [f"# freshpin live accuracy check ({time.strftime('%Y-%m-%d %H:%M %Z')})", ""]
    by_eco: dict[str, list[Row]] = {}
    for r in rows:
        by_eco.setdefault(r.ecosystem, []).append(r)
    out += ["| Ecosystem | Checked | Match | Mismatch | n/a |", "|---|---|---|---|---|"]
    for eco, items in by_eco.items():
        c = [r.verdict for r in items]
        out.append(
            f"| {eco} | {len(items)} | {c.count('match')} | {c.count('MISMATCH')} | {c.count('n/a')} |"
        )
    ok = [x for x in docker_rows if x[2]]
    bad = [x for x in docker_rows if x[2] is False]
    unk = [x for x in docker_rows if x[2] is None]
    out.append(f"| docker tags exist | {len(docker_rows)} | {len(ok)} | {len(bad)} | {len(unk)} |")
    out += ["", f"Elapsed: {elapsed:.1f}s", "", "## Differences and gaps", ""]
    diffs = [r for r in rows if r.verdict != "match"]
    if not diffs and not bad and not unk:
        out.append("None.")
    for r in diffs:
        out.append(
            f"- `{r.ecosystem}:{r.name}` freshpin={r.freshpin} truth={r.truth} {r.note}".rstrip()
        )
    for img, tag, okv in bad + unk:
        out.append(
            f"- docker `{img}:{tag}` {'does not exist on Docker Hub' if okv is False else 'could not be checked'}"
        )
    out += [
        "",
        "## All results",
        "",
        "| Ecosystem | Name | freshpin | Independent source | Verdict |",
        "|---|---|---|---|---|",
    ]
    out += [f"| {r.ecosystem} | {r.name} | {r.freshpin} | {r.truth} | {r.verdict} |" for r in rows]
    out += ["", "| Docker image | Suggested tag | Exists |", "|---|---|---|"]
    out += [
        f"| {i} | {t} | {'yes' if o else ('NO' if o is False else '?')} |"
        for i, t, o in docker_rows
    ]
    return out


if __name__ == "__main__":
    raise SystemExit(main())
