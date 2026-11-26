from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from freshdeps.cache import Cache, default_cache_dir, default_ttl, parse_duration


def test_set_get_flush_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    cache = Cache(path, ttl=60)
    assert cache.get("pypi:requests") is None
    cache.set("pypi:requests", {"version": "2.32.5"})
    entry = cache.get("pypi:requests")
    assert entry is not None and entry.fresh and entry.data == {"version": "2.32.5"}
    assert not path.exists()  # writes are batched until flush
    cache.flush()
    on_disk = json.loads(path.read_text())
    assert on_disk["version"] == 1
    assert on_disk["entries"]["pypi:requests"]["data"] == {"version": "2.32.5"}

    reloaded = Cache(path, ttl=60).get("pypi:requests")
    assert reloaded is not None and reloaded.data["version"] == "2.32.5"
    assert reloaded.age < 5


def test_expired_entries_are_returned_but_not_fresh(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    path.write_text(
        json.dumps(
            {"version": 1, "entries": {"npm:react": {"ts": time.time() - 100, "data": {"v": 1}}}}
        )
    )
    entry = Cache(path, ttl=10).get("npm:react")
    assert entry is not None and not entry.fresh and entry.data == {"v": 1}


def test_disabled_cache_is_a_noop(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    cache = Cache(path, enabled=False)
    cache.set("k", {"a": 1})
    cache.flush()
    assert cache.get("k") is None
    assert not path.exists()


@pytest.mark.parametrize("content", ["not json", "[]", '{"version": 99, "entries": {}}'])
def test_corrupt_cache_is_ignored(tmp_path: Path, content: str) -> None:
    path = tmp_path / "cache.json"
    path.write_text(content)
    cache = Cache(path)
    assert cache.get("anything") is None
    cache.set("k", {"a": 1})
    cache.flush()
    assert json.loads(path.read_text())["entries"]["k"]["data"] == {"a": 1}


def test_flush_merges_with_concurrent_writer(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    first = Cache(path)
    second = Cache(path)
    first.set("a", {"v": 1})
    second.set("b", {"v": 2})
    first.flush()
    second.flush()
    entries = json.loads(path.read_text())["entries"]
    assert set(entries) == {"a", "b"}


def test_thread_safety(tmp_path: Path) -> None:
    cache = Cache(tmp_path / "cache.json")

    def worker(n: int) -> None:
        for i in range(50):
            cache.set(f"k{n}-{i}", {"i": i})
            cache.get(f"k{n}-{i}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    cache.flush()
    assert len(json.loads((tmp_path / "cache.json").read_text())["entries"]) == 400


def test_clear(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    cache = Cache(path)
    cache.set("a", {})
    cache.set("b", {})
    cache.flush()
    assert cache.clear() == 2
    assert not path.exists()
    assert cache.get("a") is None


def test_unwritable_location_never_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    cache = Cache(blocker / "sub" / "cache.json")
    cache.set("a", {})
    cache.flush()  # parent "directory" is a file -> OSError swallowed


def test_parse_duration() -> None:
    assert parse_duration("3600") == 3600
    assert parse_duration("30m") == 1800
    assert parse_duration("6h") == 21600
    assert parse_duration("1d") == 86400
    assert parse_duration("1.5H") == 5400
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_env_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRESHDEPS_CACHE_DIR", str(tmp_path / "x"))
    assert default_cache_dir() == tmp_path / "x"
    monkeypatch.delenv("FRESHDEPS_CACHE_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert default_cache_dir() == tmp_path / "freshdeps"
    monkeypatch.setenv("FRESHDEPS_CACHE_TTL", "2h")
    assert default_ttl() == 7200
    monkeypatch.setenv("FRESHDEPS_CACHE_TTL", "garbage")
    assert default_ttl() == 6 * 3600
