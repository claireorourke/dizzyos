"""Cached data fetching, shared by all apps.

The render loop must never block on the network, so apps fetch through this service
from `refresh()` (which the launcher calls off the hot path) and read the cached
result in `render()`. Fetches are cached with a TTL and are stale-on-error: if a
refresh fails, the last good value keeps showing instead of a blank screen.

Two flavors, same caching: `get_json` for JSON feeds and `get_bytes` for binary ones
(e.g. the MTA's protobuf GTFS-realtime feeds — the subway app decodes those itself).

Supports http(s) URLs and local file paths (handy for offline dev against a
locally-built `izzys-cafe.json`). Uses only the standard library.
"""

import json
import threading
import time
import urllib.request


class DataService:
    def __init__(self, clock=time.monotonic, log=lambda _m: None):
        self._cache = {}  # (kind, url) -> (fetched_at, value)
        self._lock = threading.Lock()
        self._clock = clock
        self._log = log

    def get_json(self, url, ttl=300, fallback=None, headers=None):
        """Return parsed JSON for `url`, refetching only once older than `ttl`."""
        return self._get("json", url, ttl, fallback, headers)

    def get_bytes(self, url, ttl=300, fallback=None, headers=None):
        """Return the raw `bytes` at `url`, refetching only once older than `ttl`."""
        return self._get("bytes", url, ttl, fallback, headers)

    def _get(self, kind, url, ttl, fallback, headers=None):
        """Cached fetch. On failure: return the cached value if we have one, else
        `fallback` if provided, else re-raise."""
        key = (kind, url)
        now = self._clock()
        with self._lock:
            entry = self._cache.get(key)
        if entry and (now - entry[0]) < ttl:
            return entry[1]

        try:
            value = self._fetch(kind, url, headers)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully on any error
            self._log(f"data: fetch failed for {url}: {exc}")
            if entry is not None:
                return entry[1]
            if fallback is not None:
                return fallback
            raise

        with self._lock:
            self._cache[key] = (now, value)
        return value

    @staticmethod
    def _fetch(kind, url, headers=None):
        if url.startswith(("http://", "https://")):
            # `headers` is not part of the cache key: a given URL is always fetched
            # from one call site, so the same headers ride along with it every time.
            request = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(request, timeout=10) as resp:
                raw = resp.read()
        else:
            with open(url, "rb") as handle:
                raw = handle.read()
        return raw if kind == "bytes" else json.loads(raw.decode("utf-8"))
