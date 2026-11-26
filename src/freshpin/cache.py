"""A tiny thread-safe JSON TTL cache stored at ``~/.cache/freshpin/cache.json``.

* Reads are served from memory after one lazy load.
* Writes are batched in memory and flushed atomically (temp file + ``os.replace``),
  merging with whatever another freshpin process wrote in the meantime.
* Expired entries are kept on disk so lookups can fall back to them when offline.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TTL_SECONDS = 6 * 60 * 60
CACHE_FILENAME = "cache.json"
# Bump when cached registry data changes shape, so stale answers are never reused.
# 2: GitHub entries carry the repository's floating refs ("floating", "refs_complete").
_SCHEMA_VERSION = 2


def default_cache_dir() -> Path:
    """``$FRESHPIN_CACHE_DIR`` > ``$XDG_CACHE_HOME/freshpin`` > ``~/.cache/freshpin``."""
    explicit = os.environ.get("FRESHPIN_CACHE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "freshpin"


_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> float:
    """Parse ``"6h"``, ``"30m"``, ``"90s"``, ``"1d"`` or plain seconds into seconds."""
    m = _DURATION_RE.match(text)
    if not m:
        raise ValueError(f"invalid duration {text!r} (examples: 3600, 30m, 6h, 1d)")
    return float(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]


def default_ttl() -> float:
    env = os.environ.get("FRESHPIN_CACHE_TTL")
    if env:
        try:
            return parse_duration(env)
        except ValueError:
            return DEFAULT_TTL_SECONDS
    return DEFAULT_TTL_SECONDS


@dataclass(frozen=True)
class CacheEntry:
    data: dict[str, Any]
    stored_at: float
    fresh: bool

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.stored_at)


class Cache:
    """Disk-backed TTL cache. ``enabled=False`` turns every operation into a no-op."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        ttl: float | None = None,
        enabled: bool = True,
    ) -> None:
        self.path = path if path is not None else default_cache_dir() / CACHE_FILENAME
        self.ttl = default_ttl() if ttl is None else ttl
        self.enabled = enabled
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] | None = None
        self._dirty: set[str] = set()

    # -- internals -------------------------------------------------------
    def _read_disk(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict) or raw.get("version") != _SCHEMA_VERSION:
            return {}
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            return {}
        return {
            k: v
            for k, v in entries.items()
            if isinstance(k, str)
            and isinstance(v, dict)
            and isinstance(v.get("ts"), (int, float))
            and isinstance(v.get("data"), dict)
        }

    def _loaded(self) -> dict[str, dict[str, Any]]:
        if self._entries is None:
            self._entries = self._read_disk()
        return self._entries

    # -- public API --------------------------------------------------------
    def get(self, key: str) -> CacheEntry | None:
        """Return the entry for ``key`` (fresh or expired), or ``None``."""
        if not self.enabled:
            return None
        with self._lock:
            entry = self._loaded().get(key)
        if entry is None:
            return None
        stored_at = float(entry["ts"])
        fresh = (time.time() - stored_at) < self.ttl
        return CacheEntry(data=dict(entry["data"]), stored_at=stored_at, fresh=fresh)

    def set(self, key: str, data: dict[str, Any]) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._loaded()[key] = {"ts": time.time(), "data": data}
            self._dirty.add(key)

    def flush(self) -> None:
        """Atomically persist pending writes, merging with concurrent writers."""
        if not self.enabled:
            return
        with self._lock:
            if not self._dirty:
                return
            mine = self._loaded()
            merged = self._read_disk()
            for key in self._dirty:
                ours = mine[key]
                theirs = merged.get(key)
                if theirs is None or float(theirs["ts"]) <= float(ours["ts"]):
                    merged[key] = ours
            payload = {"version": _SCHEMA_VERSION, "entries": merged}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".cache-", suffix=".json", dir=self.path.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
                    Path(tmp).replace(self.path)
                except BaseException:
                    Path(tmp).unlink(missing_ok=True)
                    raise
            except OSError:
                return  # a read-only home directory must never break a lookup
            self._entries = merged
            self._dirty.clear()

    def clear(self) -> int:
        """Delete the cache file. Returns the number of entries removed."""
        with self._lock:
            count = len(self._read_disk())
            with contextlib.suppress(OSError):
                self.path.unlink(missing_ok=True)
            self._entries = {}
            self._dirty.clear()
            return count
