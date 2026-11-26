from __future__ import annotations

import pytest

from freshpin.versioning import (
    ConstraintError,
    Version,
    assess,
    bump_kind,
    is_breaking,
    is_stable,
    max_version,
    meets_level,
    parse_pep440,
    parse_semver_range,
    parse_version,
    rewrite_spec,
    truncate,
)


def v(text: str) -> Version:
    parsed = parse_version(text)
    assert parsed is not None, text
    return parsed


# --- parsing & ordering ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "release", "pre"),
    [
        ("1.2.3", (1, 2, 3), False),
        ("v4", (4,), False),
        ("V4.1.0", (4, 1, 0), False),
        ("2.0.0rc1", (2, 0, 0), True),
        ("2.0.0-beta.2", (2, 0, 0), True),
        ("1.0.dev3", (1, 0), True),
        ("1.0a1", (1, 0), True),
        ("5.0.0-canary.12", (5, 0, 0), True),
        ("1.0.post2", (1, 0), False),
        ("1.2.3+build.5", (1, 2, 3), False),
        ("1!2.0", (2, 0), False),
    ],
)
def test_parse_version(text: str, release: tuple[int, ...], pre: bool) -> None:
    parsed = v(text)
    assert parsed.release == release
    assert parsed.is_prerelease is pre


@pytest.mark.parametrize("text", ["", "latest", "main", "release/v1", "abc", "x.y"])
def test_parse_version_rejects_non_versions(text: str) -> None:
    assert parse_version(text) is None


def test_ordering_follows_pep440_and_semver() -> None:
    ordered = [
        "1.0.dev1",
        "1.0a1",
        "1.0b2",
        "1.0rc1",
        "1.0",
        "1.0.post1",
        "1.0.1",
        "1.2",
        "1.10",
        "2.0.0-alpha",
        "2.0.0",
    ]
    parsed = [v(t) for t in ordered]
    assert parsed == sorted(parsed)
    assert v("1.0") == v("1.0.0") == v("v1")
    assert hash(v("1.0")) == hash(v("1.0.0"))
    assert v("1.10.0") > v("1.9.9")
    assert v("2") != "2"  # comparing to a non-Version is not equality


def test_is_stable_and_max_version() -> None:
    assert is_stable("2.32.3")
    assert not is_stable("3.0.0rc1")
    assert not is_stable("nonsense")
    candidates = ["1.9.0", "2.0.0rc1", "1.10.2", "garbage", "1.10.1"]
    assert max_version(candidates) == "1.10.2"
    assert max_version(candidates, include_pre=True) == "2.0.0rc1"
    assert max_version([]) is None


def test_truncate_and_bumps() -> None:
    assert truncate(v("3.14.2"), 2) == v("3.14")
    assert truncate(v("3.14.2"), 2).raw == "3.14"
    assert truncate(v("7"), 3).raw == "7.0.0"
    assert bump_kind(v("1.2.3"), v("2.0.0")) == "major"
    assert bump_kind(v("1.2.3"), v("1.3.0")) == "minor"
    assert bump_kind(v("1.2.3"), v("1.2.4")) == "patch"
    assert bump_kind(v("1.2.3"), v("1.2.3")) is None
    assert bump_kind(v("2.0"), v("1.9")) is None
    assert is_breaking(v("0.100.0"), v("0.121.0"))
    assert is_breaking(v("4.2"), v("5.0"))
    assert not is_breaking(v("2.28"), v("2.32.5"))
    assert meets_level("major", "minor")
    assert not meets_level("patch", "minor")
    assert not meets_level(None, "patch")


# --- PEP 440 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "allowed", "rejected", "floor"),
    [
        (">=1.4,<2", ["1.4", "1.9.9"], ["1.3", "2.0"], "1.4"),
        ("~=4.2", ["4.2", "4.9"], ["5.0", "4.1"], "4.2"),
        ("~=1.4.5", ["1.4.9"], ["1.5.0"], "1.4.5"),
        ("==2.*", ["2.0", "2.99"], ["3.0"], "2"),
        ("==2.28.0", ["2.28.0", "2.28"], ["2.28.1"], "2.28.0"),
        ("!=1.5.*,>=1", ["1.4", "1.6"], ["1.5.2"], "1"),
        ("<2", ["1.9"], ["2.0"], None),
        ("(>=3.0)", ["3.1"], ["2.9"], "3.0"),
    ],
)
def test_parse_pep440(
    spec: str, allowed: list[str], rejected: list[str], floor: str | None
) -> None:
    c = parse_pep440(spec)
    for item in allowed:
        assert c.allows(v(item)), (spec, item)
    for item in rejected:
        assert not c.allows(v(item)), (spec, item)
    assert (c.floor.raw if c.floor else None) == floor


def test_parse_pep440_empty_and_invalid() -> None:
    assert parse_pep440("").is_unbounded
    with pytest.raises(ConstraintError):
        parse_pep440("=>1.0")
    with pytest.raises(ConstraintError):
        parse_pep440("~=2")
    with pytest.raises(ConstraintError):
        parse_pep440(">=1.*")


# --- SemVer ranges --------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "bare", "allowed", "rejected", "floor"),
    [
        ("^18.2.0", "exact", ["18.2.0", "18.9.1"], ["19.0.0", "18.1.0"], "18.2.0"),
        ("^0.2.3", "exact", ["0.2.9"], ["0.3.0"], "0.2.3"),
        ("^0.0.3", "exact", ["0.0.3"], ["0.0.4"], "0.0.3"),
        ("~1.2.3", "exact", ["1.2.9"], ["1.3.0"], "1.2.3"),
        ("~1", "exact", ["1.9.0"], ["2.0.0"], "1"),
        ("1.2.x", "exact", ["1.2.7"], ["1.3.0"], "1.2"),
        ("18.x", "exact", ["18.0.1"], ["19.0.0"], "18"),
        ("1.2.3", "exact", ["1.2.3"], ["1.2.4"], "1.2.3"),
        ("1.2.3", "caret", ["1.9.0"], ["2.0.0"], "1.2.3"),
        ("0.11", "caret", ["0.11.27"], ["0.12.0"], "0.11"),
        ("1", "caret", ["1.0.229"], ["2.0.0"], "1"),
        (">=1.0.0 <3", "exact", ["2.9.9"], ["3.0.0"], "1.0.0"),
        (">= 1.2, < 1.5", "caret", ["1.4.0"], ["1.5.0"], "1.2"),
        ("^16 || ^18", "exact", ["16.4.0", "18.1.0"], ["17.0.0", "19.0.0"], "18"),
        ("1.2.3 - 2.3", "exact", ["2.3.9"], ["2.4.0"], "1.2.3"),
        ("=1.2", "caret", ["1.2.5"], ["1.3.0"], "1.2"),
        ("==1.2.3", "exact", ["1.2.3"], ["1.2.4"], "1.2.3"),
        ("~> 2.1", "exact", ["2.9"], ["3.0"], "2.1"),
        (">1.2", "exact", ["1.3.0"], ["1.2.9"], "1.3"),
        ("<=1.2", "exact", ["1.2.9"], ["1.3.0"], None),
    ],
)
def test_parse_semver_range(
    spec: str, bare: str, allowed: list[str], rejected: list[str], floor: str | None
) -> None:
    c = parse_semver_range(spec, bare="caret" if bare == "caret" else "exact")
    for item in allowed:
        assert c.allows(v(item)), (spec, item)
    for item in rejected:
        assert not c.allows(v(item)), (spec, item)
    assert (c.floor.raw if c.floor else None) == floor


@pytest.mark.parametrize("spec", ["*", "", "x", "latest", ">=1 || *"])
def test_semver_unbounded(spec: str) -> None:
    assert parse_semver_range(spec).is_unbounded


def test_semver_invalid() -> None:
    with pytest.raises(ConstraintError):
        parse_semver_range("not-a-version")


# --- assessment & rewriting ---------------------------------------------------------


def test_assess_excluding_constraint_is_outdated() -> None:
    a = assess(parse_semver_range("^18.2.0"), v("19.1.0"))
    assert a.outdated and a.bump == "major" and a.excludes_latest


def test_assess_patch_drift_respects_level() -> None:
    c = parse_pep440("==2.32.3")
    assert not assess(c, v("2.32.5")).outdated
    patch = assess(c, v("2.32.5"), level="patch")
    assert patch.outdated and patch.bump == "patch"


def test_assess_stale_floor_is_outdated_only_when_breaking() -> None:
    assert assess(parse_pep440(">=0.100.0"), v("0.121.3")).outdated
    assert assess(parse_pep440(">=4.2"), v("5.2.8")).outdated
    allowed = assess(parse_pep440(">=2.28"), v("2.32.5"))
    assert not allowed.outdated and allowed.bump == "minor"


def test_assess_without_floor_check_is_library_friendly() -> None:
    stale_floor = parse_pep440(">=4.2")
    assert assess(stale_floor, v("5.2.8")).outdated
    assert not assess(stale_floor, v("5.2.8"), floors=False).outdated
    assert assess(parse_pep440("<5"), v("5.2.8"), floors=False).outdated  # still excludes


def test_assess_cap_without_floor() -> None:
    a = assess(parse_pep440("<2"), v("2.11.4"))
    assert a.outdated and a.excludes_latest


def test_assess_unbounded_and_future_prerelease_pin() -> None:
    assert not assess(parse_pep440(""), v("1.0")).outdated
    assert not assess(parse_pep440("==3.0.0rc1"), v("2.9.0")).outdated


@pytest.mark.parametrize(
    ("spec", "parser", "latest", "expected"),
    [
        ("^18.2.0", "npm", "19.1.0", "^19.1.0"),
        ("~=4.2", "pep", "5.2.8", "~=5.2"),
        (">=0.100.0", "pep", "0.121.3", ">=0.121.3"),
        ("18.x", "npm", "19.2.0", "19.x"),
        (">=1.10,<2", "pep", "2.11.4", ">=2.11.4"),
        ("0.11", "cargo", "0.13.1", "0.13"),
        # exact pins always name the full latest version (post-releases included)
        ("==4.3.post1", "pep", "4.5.post3", "==4.5.post3"),
        ("==2.28", "pep", "2.32.5", "==2.32.5"),
        ("14.2.3", "npm", "16.0.1", "16.0.1"),
        # only the floor is stale: keep the cap that still admits the latest release
        (">=7.0,<9.0.0", "pep", "8.5.1", ">=8.5,<9.0.0"),
        (">=2,<4", "pep", "3.5.3", ">=3,<4"),
        (">=1.0,<11.0", "pep", "10.2.0", ">=10.2,<11.0"),
        (">=1.2.0 <3", "npm", "2.4.0", ">=2.4.0 <3"),
    ],
)
def test_rewrite_spec(spec: str, parser: str, latest: str, expected: str) -> None:
    if parser == "pep":
        c = parse_pep440(spec)
        op = ">="
    elif parser == "cargo":
        c = parse_semver_range(spec, bare="caret")
        op = ""
    else:
        c = parse_semver_range(spec)
        op = "^"
    assert rewrite_spec(c, v(latest), fallback_op=op) == expected
