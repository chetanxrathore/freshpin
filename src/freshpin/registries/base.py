"""Registry protocol, resolution result type and a small stdlib HTTP client."""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import urlsplit

from freshpin import __version__

USER_AGENT = f"freshpin/{__version__} (python-urllib; dependency version checker)"
DEFAULT_TIMEOUT = 5.0
DEFAULT_RETRIES = 2
_RETRY_STATUS = {429, 500, 502, 503, 504}
# Be polite to APIs with strict crawler policies.
_HOST_CONCURRENCY = {"crates.io": 2, "api.github.com": 4, "hub.docker.com": 4}
_DEFAULT_HOST_CONCURRENCY = 8


class RegistryError(Exception):
    """A lookup failed (network, HTTP or payload problem)."""


class NotFoundError(RegistryError):
    """The package / repository / product does not exist on the registry."""


class HttpError(RegistryError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RateLimitError(HttpError):
    """An API quota is exhausted (callers may fall back to another endpoint)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface 3xx responses as errors so their ``Location`` can be read."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)
_REDIRECT_STATUS = {301, 302, 303, 307, 308}


class HttpClient:
    """Minimal HTTPS client: timeouts, retries with backoff, per-host limits, a clear UA."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        backoff: float = 0.5,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = backoff
        self.user_agent = user_agent
        self._host_locks: dict[str, threading.BoundedSemaphore] = {}
        self._locks_guard = threading.Lock()

    def _semaphore(self, host: str) -> threading.BoundedSemaphore:
        with self._locks_guard:
            sem = self._host_locks.get(host)
            if sem is None:
                sem = threading.BoundedSemaphore(
                    _HOST_CONCURRENCY.get(host, _DEFAULT_HOST_CONCURRENCY)
                )
                self._host_locks[host] = sem
            return sem

    def _fetch(
        self,
        url: str,
        headers: Mapping[str, str] | None,
        *,
        accept: str,
        follow_redirects: bool = True,
    ) -> bytes | str:
        """GET ``url``; returns the body, or the ``Location`` (str) of an unfollowed 3xx."""
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise HttpError(f"refusing non-HTTPS URL: {url}")
        host = parts.hostname
        request_headers = {"User-Agent": self.user_agent, "Accept": accept}
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(url, headers=request_headers, method="GET")
        last_error = "unknown error"
        with self._semaphore(host):
            for attempt in range(self.retries + 1):
                if attempt:
                    time.sleep(self.backoff * (2 ** (attempt - 1)))
                try:
                    if follow_redirects:
                        response = urllib.request.urlopen(request, timeout=self.timeout)
                    else:
                        response = _NO_REDIRECT_OPENER.open(request, timeout=self.timeout)
                    with response:
                        return bytes(response.read())
                except urllib.error.HTTPError as exc:
                    if exc.code in _REDIRECT_STATUS and not follow_redirects:
                        return str(exc.headers.get("Location") or "")
                    if exc.code == 404:
                        raise NotFoundError(f"not found ({host})") from None
                    if exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0":
                        raise RateLimitError(
                            "GitHub API rate limit exceeded; set GITHUB_TOKEN to raise it",
                            exc.code,
                        ) from None
                    last_error = f"HTTP {exc.code} from {host}"
                    if exc.code in _RETRY_STATUS and attempt < self.retries:
                        continue
                    raise HttpError(last_error, exc.code) from None
                except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                    reason = getattr(exc, "reason", exc)
                    last_error = f"network error contacting {host}: {reason}"
                    if attempt < self.retries:
                        continue
                    raise HttpError(last_error) from None
        raise HttpError(last_error)  # pragma: no cover - loop always returns or raises

    def get_json(self, url: str, headers: Mapping[str, str] | None = None) -> Any:
        """GET ``url`` and decode JSON. Raises :class:`NotFoundError` / :class:`HttpError`."""
        body = self._fetch(url, headers, accept="application/json")
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise HttpError(f"invalid JSON from {urlsplit(url).hostname}") from None

    def get_text(self, url: str, headers: Mapping[str, str] | None = None) -> str:
        """GET ``url`` and return the body as text."""
        body = self._fetch(url, headers, accept="*/*")
        return body.decode("utf-8", "replace") if isinstance(body, bytes) else body

    def get_redirect(self, url: str, headers: Mapping[str, str] | None = None) -> str | None:
        """Return where ``url`` redirects to (``None`` if it answers without redirecting)."""
        result = self._fetch(url, headers, accept="text/html", follow_redirects=False)
        return result if isinstance(result, str) and result else None


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving one ``ecosystem:name`` query."""

    ecosystem: str
    name: str
    query: str
    version: str | None = None
    url: str | None = None
    error: str | None = None
    cached: bool = False
    stale: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.version is not None and self.error is None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "query": self.query,
            "ecosystem": self.ecosystem,
            "name": self.name,
            "version": self.version,
            "url": self.url,
            "cached": self.cached,
        }
        if self.stale:
            out["stale"] = True
        if self.error:
            out["error"] = self.error
        if self.details:
            out["details"] = dict(self.details)
        return out


class Registry(ABC):
    """A source of truth for "what is the latest stable version of X?"."""

    ecosystem: ClassVar[str]
    aliases: ClassVar[tuple[str, ...]] = ()
    title: ClassVar[str] = ""
    example: ClassVar[str] = ""

    def normalize(self, name: str) -> str:
        """Canonical form of ``name`` (used for cache keys and display)."""
        return name.strip()

    @abstractmethod
    def fetch(self, name: str, http: HttpClient, *, include_pre: bool = False) -> dict[str, Any]:
        """Query the registry. Must return a JSON-serialisable dict with a ``version`` key
        (and optionally ``url`` and ``details``). Raise :class:`RegistryError` on failure."""


def as_dict(value: Any) -> dict[str, Any]:
    """Narrow a decoded JSON value to a dict (empty if it is something else)."""
    return value if isinstance(value, dict) else {}


_UNSAFE_TEXT_RE = re.compile(r"[\x00-\x1f\x7f`<>]+")


def clean_text(value: Any, limit: int = 160) -> str | None:
    """Registry-provided prose (summaries, deprecation notices) is untrusted: flatten it to
    one short line without control characters, backticks or angle brackets before it can
    reach an agent's context."""
    if not isinstance(value, str):
        return None
    text = " ".join(_UNSAFE_TEXT_RE.sub(" ", value).split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
