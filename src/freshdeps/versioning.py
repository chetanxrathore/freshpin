"""Version parsing, comparison and constraint evaluation.

Deliberately dependency-free. It understands enough of PEP 440 (Python) and SemVer
ranges (npm, Cargo, Poetry) to answer the two questions freshdeps cares about:

* "Is this version a stable release?" (pre-releases are excluded by default)
* "Does this constraint, as written, still allow the latest stable release, and how
  far behind is its floor?"
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import total_ordering
from typing import Literal

Bump = Literal["major", "minor", "patch"]
BUMP_RANK: dict[str, int] = {"patch": 1, "minor": 2, "major": 3}

_FINAL_RANK = 9
_PRE_RANKS: dict[str, int] = {
    "dev": 0,
    "snapshot": 0,
    "nightly": 0,
    "canary": 0,
    "insiders": 0,
    "next": 0,
    "experimental": 0,
    "exp": 0,
    "a": 1,
    "alpha": 1,
    "b": 2,
    "beta": 2,
    "c": 3,
    "rc": 3,
    "cr": 3,
    "pre": 3,
    "preview": 3,
}

_CORE_RE = re.compile(r"^(?:(\d+)!)?(\d+(?:\.\d+)*)(.*)$")
_POST_RE = re.compile(r"^[-_.]?(?:post|rev|r)[-_.]?(\d*)$", re.IGNORECASE)
_SUFFIX_RE = re.compile(r"^[-_.]?([a-z]+)[-_.]?(\d*)(.*)$", re.IGNORECASE)


@total_ordering
@dataclass(frozen=True, eq=False)
class Version:
    """A leniently parsed version (PEP 440 or SemVer flavoured)."""

    raw: str
    release: tuple[int, ...]
    epoch: int = 0
    pre: tuple[int, int, str] | None = None
    post: int | None = None
    local: str | None = None
    segments: tuple[str, ...] = ()  # release components exactly as written ("26", "04")

    @property
    def is_prerelease(self) -> bool:
        return self.pre is not None

    @property
    def major(self) -> int:
        return self.release[0]

    @property
    def minor(self) -> int:
        return self.release[1] if len(self.release) > 1 else 0

    @property
    def patch(self) -> int:
        return self.release[2] if len(self.release) > 2 else 0

    @property
    def precision(self) -> int:
        """Number of release components as written (``3.12`` -> 2)."""
        return len(self.release)

    @property
    def base(self) -> str:
        """The release segment only, e.g. ``1.2.3`` for ``v1.2.3-rc.1`` (``24.04`` stays)."""
        return ".".join(self.segments or tuple(str(p) for p in self.release))

    def _key(self) -> tuple[int, tuple[int, ...], tuple[int, int, str], int]:
        rel = list(self.release)
        while len(rel) > 1 and rel[-1] == 0:
            rel.pop()
        pre = self.pre if self.pre is not None else (_FINAL_RANK, 0, "")
        post = self.post if self.post is not None else -1
        return (self.epoch, tuple(rel), pre, post)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() == other._key()

    def __lt__(self, other: Version) -> bool:
        return self._key() < other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __str__(self) -> str:
        return self.raw


def parse_version(text: str) -> Version | None:
    """Parse ``text`` into a :class:`Version`, or return ``None`` if it is not one.

    Accepts a leading ``v`` (``v4.1.0``), PEP 440 suffixes (``1.0rc1``, ``2.0.dev3``,
    ``1.0.post1``), SemVer pre-releases (``1.0.0-beta.2``) and build metadata (``+abc``).
    """
    s = text.strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    s, _, local = s.partition("+")
    m = _CORE_RE.match(s)
    if not m:
        return None
    epoch = int(m.group(1) or 0)
    segments = tuple(m.group(2).split("."))
    release = tuple(int(p) for p in segments)
    rest = m.group(3)
    pre: tuple[int, int, str] | None = None
    post: int | None = None
    if rest:
        pm = _POST_RE.match(rest)
        if pm:
            post = int(pm.group(1) or 0)
        else:
            sm = _SUFFIX_RE.match(rest)
            if sm:
                label = sm.group(1).lower()
                num = int(sm.group(2)) if sm.group(2) else 0
                pre = (_PRE_RANKS.get(label, 1), num, sm.group(3).lower())
            else:
                pre = (1, 0, rest.lower())
    return Version(
        raw=text.strip(),
        release=release,
        epoch=epoch,
        pre=pre,
        post=post,
        local=local or None,
        segments=segments,
    )


def is_stable(text: str) -> bool:
    """True when ``text`` parses as a version and is not a pre-release."""
    v = parse_version(text)
    return v is not None and not v.is_prerelease


def max_version(candidates: Iterable[str], *, include_pre: bool = False) -> str | None:
    """Return the highest version string in ``candidates`` (stable-only by default)."""
    best: Version | None = None
    for candidate in candidates:
        v = parse_version(candidate)
        if v is None or (v.is_prerelease and not include_pre):
            continue
        if best is None or v > best:
            best = v
    return best.raw if best is not None else None


def truncate(version: Version, precision: int) -> Version:
    """Keep only the first ``precision`` release components (``3.14.2`` -> ``3.14``)."""
    precision = max(1, precision)
    rel = (*version.release, 0, 0, 0)[:precision]
    written = version.segments or tuple(str(p) for p in version.release)
    segments = (*written, "0", "0", "0")[:precision]
    text = ".".join(segments)
    return Version(raw=text, release=tuple(rel), epoch=version.epoch, segments=segments)


def bump_kind(current: Version, latest: Version) -> Bump | None:
    """Classify how far ``latest`` is ahead of ``current`` (``None`` if not ahead)."""
    if latest <= current:
        return None
    a = (*current.release, 0, 0)
    b = (*latest.release, 0, 0)
    if b[0] != a[0]:
        return "major"
    if b[1] != a[1]:
        return "minor"
    return "patch"


def is_breaking(current: Version, latest: Version) -> bool:
    """SemVer-breaking distance: a major bump, or a minor bump while still on ``0.x``."""
    kind = bump_kind(current, latest)
    return kind == "major" or (kind == "minor" and current.major == 0)


def meets_level(bump: Bump | None, level: Bump) -> bool:
    """True if ``bump`` is at least as significant as ``level``."""
    return bump is not None and BUMP_RANK[bump] >= BUMP_RANK[level]


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Comparator:
    """A single ``op version`` test. ``op`` is one of == != >= <= > < or ``!prefix``."""

    op: str
    version: Version

    def matches(self, v: Version) -> bool:
        if self.op == "!prefix":
            return truncate(v, self.version.precision) != self.version
        if self.op == "==":
            return v == self.version
        if self.op == "!=":
            return v != self.version
        if self.op == ">=":
            return v >= self.version
        if self.op == "<=":
            return v <= self.version
        if self.op == ">":
            return v > self.version
        if self.op == "<":
            return v < self.version
        raise ValueError(f"unknown operator {self.op!r}")


@dataclass(frozen=True)
class Constraint:
    """A parsed version constraint: an OR of AND-ed comparator groups.

    ``alternatives == ()`` means "anything goes" (``*``, ``latest``, bare name).
    ``floor`` is the lowest version the author explicitly targets (the pinned version,
    or the lower bound) - the thing we compare against the latest release.
    ``clauses`` counts the comparators as written, so callers know whether the spec
    is simple enough to rewrite in place.
    """

    raw: str
    alternatives: tuple[tuple[Comparator, ...], ...]
    floor: Version | None
    clauses: int

    @property
    def is_unbounded(self) -> bool:
        return not self.alternatives

    def allows(self, v: Version) -> bool:
        if not self.alternatives:
            return True
        return any(all(c.matches(v) for c in group) for group in self.alternatives)


class ConstraintError(ValueError):
    """Raised when a version constraint cannot be understood."""


def _v(text: str) -> Version:
    parsed = parse_version(text)
    if parsed is None:
        raise ConstraintError(f"not a version: {text!r}")
    return parsed


def _bump_at(version: Version, index: int) -> Version:
    """Increment component ``index`` and drop everything after it (for upper bounds)."""
    rel = list((*version.release, 0, 0, 0)[: max(index + 1, 1)])
    rel[index] += 1
    text = ".".join(str(p) for p in rel)
    return Version(raw=text, release=tuple(rel))


def _floor_of(groups: list[tuple[Comparator, ...]]) -> Version | None:
    floor: Version | None = None
    for group in groups:
        lows = [c.version for c in group if c.op in (">=", ">", "==")]
        if not lows:
            continue
        low = max(lows)
        if floor is None or low > floor:
            floor = low
    return floor


# --- PEP 440 ---------------------------------------------------------------

_PEP440_CLAUSE_RE = re.compile(r"^\s*(===|==|!=|~=|>=|<=|>|<)\s*([^\s,;]+)\s*$")


def parse_pep440(spec: str) -> Constraint:
    """Parse a PEP 440 specifier set such as ``>=1.4,<2`` or ``~=3.2`` or ``==2.*``."""
    text = spec.strip().strip("()").strip()
    if not text:
        return Constraint(raw=spec, alternatives=(), floor=None, clauses=0)
    comparators: list[Comparator] = []
    clauses = 0
    for part in text.split(","):
        if not part.strip():
            continue
        m = _PEP440_CLAUSE_RE.match(part)
        if not m:
            raise ConstraintError(f"invalid PEP 440 clause: {part.strip()!r}")
        clauses += 1
        op, ver = m.group(1), m.group(2)
        if op == "===":
            op = "=="
        if ver.endswith(".*"):
            prefix = _v(ver[:-2])
            if op == "==":
                comparators.append(Comparator(">=", prefix))
                comparators.append(Comparator("<", _bump_at(prefix, prefix.precision - 1)))
            elif op == "!=":
                comparators.append(Comparator("!prefix", prefix))
            else:
                raise ConstraintError(f"wildcard not allowed with {op}: {part.strip()!r}")
            continue
        version = _v(ver)
        if op == "~=":
            if version.precision < 2:
                raise ConstraintError(f"~= needs at least two components: {ver!r}")
            comparators.append(Comparator(">=", version))
            comparators.append(Comparator("<", _bump_at(version, version.precision - 2)))
        else:
            comparators.append(Comparator(op, version))
    groups = [tuple(comparators)] if comparators else []
    return Constraint(
        raw=spec, alternatives=tuple(groups), floor=_floor_of(groups), clauses=clauses
    )


# --- SemVer ranges (npm / Cargo / Poetry) ------------------------------------

_SEMVER_PART_RE = re.compile(
    r"^(\^|~>|~=|~|>=|<=|>|<|==|=)?\s*v?"
    r"(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)
_HYPHEN_RE = re.compile(r"^\s*(\S+)\s+-\s+(\S+)\s*$")
_ANY = {"", "*", "x", "X", "latest"}


def _semver_partial(major: str, minor: str | None, patch: str | None, pre: str | None) -> Version:
    parts = [major]
    for p in (minor, patch):
        if p is None or p in ("x", "X", "*"):
            break
        parts.append(p)
    text = ".".join(parts) + (f"-{pre}" if pre and len(parts) == 3 else "")
    return _v(text)


def _semver_comparators(token: str, bare: str) -> list[Comparator]:
    m = _SEMVER_PART_RE.match(token.strip())
    if not m:
        raise ConstraintError(f"invalid version range: {token!r}")
    op, major, minor, patch, pre = m.groups()
    if major in ("x", "X", "*"):
        return []
    v = _semver_partial(major, minor, patch, pre)
    n = v.precision  # how many components were actually given
    if op is None:
        op = "^" if bare == "caret" else "="
    if op in ("=", "=="):
        if n == 3:
            return [Comparator("==", v)]
        return [Comparator(">=", v), Comparator("<", _bump_at(v, n - 1))]
    if op == "^":
        # Caret: don't modify the left-most non-zero component given.
        idx = 0
        for i, comp in enumerate(v.release):
            idx = i
            if comp != 0:
                break
        else:
            idx = n - 1
        return [Comparator(">=", v), Comparator("<", _bump_at(v, idx))]
    if op in ("~", "~>", "~="):
        idx = 0 if n == 1 else 1
        if op in ("~>", "~=") and n >= 2:
            idx = n - 2  # pessimistic operator (Ruby/PEP 440 flavour)
        return [Comparator(">=", v), Comparator("<", _bump_at(v, idx))]
    if op == ">" and n < 3:
        return [Comparator(">=", _bump_at(v, n - 1))]
    if op == "<=" and n < 3:
        return [Comparator("<", _bump_at(v, n - 1))]
    return [Comparator(op, v)]


def parse_semver_range(spec: str, *, bare: Literal["caret", "exact"] = "exact") -> Constraint:
    """Parse an npm/Cargo/Poetry style range.

    ``bare`` controls what an operator-less version means: npm and Poetry treat
    ``1.2.3`` as exact (``exact``), Cargo treats it as ``^1.2.3`` (``caret``).
    Supports ``||``, hyphen ranges, x-ranges, ``^``, ``~``, comparators separated by
    spaces or commas.
    """
    text = spec.strip()
    if text in _ANY:
        return Constraint(raw=spec, alternatives=(), floor=None, clauses=0)
    groups: list[tuple[Comparator, ...]] = []
    clauses = 0
    for alt in text.split("||"):
        alt = alt.strip()
        if alt in _ANY:
            return Constraint(raw=spec, alternatives=(), floor=None, clauses=0)
        comps: list[Comparator] = []
        hy = _HYPHEN_RE.match(alt)
        if hy:
            clauses += 2
            low = _semver_comparators(">=" + hy.group(1), bare)
            high = _semver_comparators("<=" + hy.group(2), bare)
            comps.extend(low + high)
        else:
            alt = re.sub(r"(\^|~>|~=|~|>=|<=|>|<|==|=)\s+", r"\1", alt)
            for token in re.split(r"[,\s]+", alt):
                if token:
                    clauses += 1
                    comps.extend(_semver_comparators(token, bare))
        if not comps:
            return Constraint(raw=spec, alternatives=(), floor=None, clauses=0)
        groups.append(tuple(comps))
    return Constraint(
        raw=spec, alternatives=tuple(groups), floor=_floor_of(groups), clauses=clauses
    )


# ---------------------------------------------------------------------------
# Assessment & rewriting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Assessment:
    outdated: bool
    bump: Bump | None
    excludes_latest: bool


def assess(constraint: Constraint, latest: Version, *, level: Bump = "minor") -> Assessment:
    """Decide whether ``constraint`` is outdated relative to ``latest``.

    A constraint is *outdated* when, and the bump is at least ``level``:

    * it does not allow the latest stable release (``^18.2.0`` vs ``19.1.0``,
      ``==2.28.0`` vs ``2.32.5``, ``<2`` vs ``2.11``), or
    * it allows it, but its floor is a SemVer-breaking distance behind
      (``fastapi>=0.100`` vs ``0.120``, ``django>=4.2`` vs ``5.2``) - the classic
      symptom of a version recalled from stale training data.
    """
    if constraint.is_unbounded:
        return Assessment(outdated=False, bump=None, excludes_latest=False)
    floor = constraint.floor
    bump = bump_kind(floor, latest) if floor is not None else None
    excluded = not constraint.allows(latest)
    if excluded and (floor is None or floor < latest):
        effective: Bump = bump or "major"
        return Assessment(
            outdated=meets_level(effective, level), bump=effective, excludes_latest=True
        )
    if floor is not None and is_breaking(floor, latest):
        return Assessment(outdated=meets_level(bump, level), bump=bump, excludes_latest=False)
    return Assessment(outdated=False, bump=bump, excludes_latest=excluded)


def rewrite_spec(
    constraint: Constraint, latest: Version, *, fallback_op: str, keep_precision: bool = True
) -> str:
    """Return ``constraint.raw`` rewritten to target ``latest``.

    Simple one-clause specs keep their operator and precision (``^18.2.0`` ->
    ``^19.1.0``, ``~=4.2`` -> ``~=5.2``, ``18.x`` -> ``19.x``). Compound specs
    (``>=1.10,<2``) are replaced by ``fallback_op`` + latest (``>=2.11.4``).
    """
    floor = constraint.floor
    precision = floor.precision if (floor is not None and keep_precision) else latest.precision
    target = truncate(latest, max(precision, 1)).raw if floor is not None else latest.base
    raw = constraint.raw.strip()
    if floor is not None and constraint.clauses == 1 and raw.count(floor.raw) == 1:
        return raw.replace(floor.raw, target)
    return f"{fallback_op}{latest.base}"
