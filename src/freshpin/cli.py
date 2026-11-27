"""Command-line interface: ``resolve``, ``audit``, ``cache-clear``, ``ecosystems``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from freshpin import __version__
from freshpin.auditors import audit_paths
from freshpin.cache import Cache, parse_duration
from freshpin.registries import (
    DOCKER_ALIASES,
    REGISTRIES,
    HttpClient,
    Query,
    Resolution,
    Resolver,
    UnknownEcosystemError,
    parse_query,
)
from freshpin.registries.dockerhub import TagChecker
from freshpin.registries.endoflife import DOCKER_IMAGES, OFFICIAL_IMAGES, PRODUCT_ALIASES
from freshpin.report import (
    FORMATS,
    dumps,
    format_audit,
    format_resolutions,
    render_table,
    use_color,
    write,
)

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2


def _duration(text: str) -> float:
    try:
        return parse_duration(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _add_output_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--format",
        "-f",
        choices=FORMATS,
        default="table",
        help="output format: human table (default), json, or compact agent text",
    )
    group.add_argument(
        "--json", action="store_const", const="json", dest="format", help="same as --format json"
    )


def _add_lookup_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pre", action="store_true", help="include pre-releases")
    parser.add_argument("--no-cache", action="store_true", help="bypass the local cache")
    parser.add_argument(
        "--offline", action="store_true", help="never touch the network; use cached data only"
    )
    parser.add_argument(
        "--ttl", type=_duration, default=None, metavar="DURATION", help="cache TTL (default 6h)"
    )
    parser.add_argument(
        "--timeout", type=float, default=5.0, metavar="SECONDS", help="HTTP timeout (default 5)"
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="skip checking that suggested Docker tags exist on Docker Hub",
    )


def build_resolver(args: argparse.Namespace) -> Resolver:
    """Construct the resolver from CLI options (tests monkeypatch this)."""
    return Resolver(
        http=HttpClient(timeout=args.timeout),
        cache=Cache(ttl=args.ttl, enabled=not args.no_cache),
        include_pre=args.pre,
        offline=args.offline,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="freshpin",
        description=(
            "Resolve and audit the latest stable versions of packages, GitHub Actions, "
            "Docker base images, databases and runtimes."
        ),
    )
    parser.add_argument("--version", action="version", version=f"freshpin {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    resolve = sub.add_parser(
        "resolve",
        help="print the latest stable version of each ecosystem:package",
        description=(
            "Examples: freshpin resolve pypi:fastapi npm:next crates:tokio "
            "gha:actions/checkout eol:postgres docker:redis"
        ),
    )
    resolve.add_argument("queries", nargs="+", metavar="ECOSYSTEM:PACKAGE")
    resolve.add_argument(
        "--ecosystem", "-e", default=None, help="default ecosystem for bare package names"
    )
    _add_output_options(resolve)
    _add_lookup_options(resolve)

    audit = sub.add_parser(
        "audit",
        help="find outdated versions in manifests, workflows and Dockerfiles",
        description=(
            "Scans pyproject.toml, requirements*.txt, package.json, Cargo.toml, "
            ".github/workflows/*.yml, action.yml, Dockerfile and docker-compose files."
        ),
    )
    audit.add_argument("paths", nargs="*", default=["."], metavar="PATH", help="files or dirs")
    audit.add_argument(
        "--fail-on-outdated",
        action="store_true",
        help="exit with status 1 if anything is outdated, missing or yanked (for CI and hooks)",
    )
    audit.add_argument(
        "--level",
        choices=("major", "minor", "patch"),
        default="minor",
        help="smallest version bump to report (default: minor)",
    )
    audit.add_argument(
        "--all", action="store_true", help="also list skipped declarations in the table"
    )
    audit.add_argument(
        "--no-floor-check",
        action="store_true",
        help="library mode: only report constraints that exclude the latest release, "
        "not ranges whose lower bound is merely old",
    )
    audit.add_argument(
        "--ignore",
        action="append",
        default=[],
        metavar="NAME",
        help="ignore a dependency by name or glob (repeatable), e.g. --ignore 'eslint*'",
    )
    audit.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="skip paths matching a glob, relative to the scanned dir (repeatable)",
    )
    _add_output_options(audit)
    _add_lookup_options(audit)

    sub.add_parser("cache-clear", help="delete the local version cache")

    eco = sub.add_parser("ecosystems", help="list supported ecosystems and aliases")
    eco.add_argument("--json", action="store_true", help="JSON output")
    return parser


def cmd_resolve(args: argparse.Namespace, out: TextIO) -> int:
    slots: list[Query | Resolution] = []
    for text in args.queries:
        try:
            slots.append(parse_query(text, args.ecosystem))
        except UnknownEcosystemError as exc:
            slots.append(Resolution(ecosystem="?", name=text, query=text, error=str(exc)))
    queries = [s for s in slots if isinstance(s, Query)]
    resolver = build_resolver(args)
    resolved = resolver.resolve_queries(queries)
    if not args.no_verify:
        resolved = verify_docker_hints(resolved, resolver)
    iterator = iter(resolved)
    results = [next(iterator) if isinstance(s, Query) else s for s in slots]
    write(format_resolutions(results, args.format, color=use_color(out)), out)
    return EXIT_OK if all(r.ok for r in results) else EXIT_FINDINGS


def verify_docker_hints(results: list[Resolution], resolver: Resolver) -> list[Resolution]:
    """Make the ``docker <image>:<tag>`` hint point at a tag that is really published."""
    checker = TagChecker(resolver.http, resolver.cache, offline=resolver.offline)

    def check(res: Resolution) -> Resolution:
        image = OFFICIAL_IMAGES.get(res.name) if res.ecosystem == "eol" and res.ok else None
        cycle = res.details.get("cycle")
        if not image or not isinstance(cycle, str) or not res.version:
            return res
        if checker.exists(image, cycle) is False and checker.exists(image, res.version):
            return replace(res, details={**res.details, "docker_tag": res.version})
        return res

    if not any(r.ecosystem == "eol" for r in results):
        return results
    with ThreadPoolExecutor(max_workers=8) as pool:
        checked = list(pool.map(check, results))
    resolver.cache.flush()
    return checked


def cmd_audit(args: argparse.Namespace, out: TextIO) -> int:
    report = audit_paths(
        [Path(p) for p in args.paths],
        build_resolver(args),
        level=args.level,
        ignore=args.ignore,
        exclude=args.exclude,
        verify_tags=not args.no_verify,
        check_floors=not args.no_floor_check,
    )
    write(format_audit(report, args.format, color=use_color(out), show_all=args.all), out)
    if not report.files and report.errors:
        return EXIT_USAGE
    if args.fail_on_outdated and report.flagged:
        return EXIT_FINDINGS
    return EXIT_OK


def cmd_cache_clear(out: TextIO) -> int:
    cache = Cache()
    removed = cache.clear()
    write(f"Removed {removed} cached entr{'y' if removed == 1 else 'ies'} ({cache.path})", out)
    return EXIT_OK


def cmd_ecosystems(args: argparse.Namespace, out: TextIO) -> int:
    rows: list[tuple[str, list[str], str, str]] = [
        (
            reg.ecosystem,
            [a for a in reg.aliases if a != reg.ecosystem],
            reg.example,
            reg.title,
        )
        for reg in REGISTRIES.values()
    ]
    rows.append(
        (
            "docker",
            [a for a in DOCKER_ALIASES if a != "docker"],
            "docker:postgres",
            "Docker image name -> endoflife.date product",
        )
    )
    if args.json:
        payload = {
            "ecosystems": [
                {"ecosystem": e, "aliases": a, "example": x, "description": d}
                for e, a, x, d in rows
            ],
            "eol_aliases": PRODUCT_ALIASES,
            "docker_images": DOCKER_IMAGES,
        }
        write(dumps(payload), out)
        return EXIT_OK
    table = render_table(
        ["ECOSYSTEM", "ALIASES", "EXAMPLE", "SOURCE"],
        [[e, ",".join(a), x, d] for e, a, x, d in rows],
        color=use_color(out),
    )
    images = ", ".join(sorted(k for k in DOCKER_IMAGES if "/" not in k))
    write(
        f"{table}\n\nTracked Docker images: {images}\nAny endoflife.date slug works with eol:", out
    )
    return EXIT_OK


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    stream = out or sys.stdout
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "resolve":
        return cmd_resolve(args, stream)
    if args.command == "audit":
        return cmd_audit(args, stream)
    if args.command == "cache-clear":
        return cmd_cache_clear(stream)
    if args.command == "ecosystems":
        return cmd_ecosystems(args, stream)
    parser.print_help(stream)
    return EXIT_USAGE
