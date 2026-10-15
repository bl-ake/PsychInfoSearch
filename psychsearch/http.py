"""Polite HTTP: per-host throttling, retries with backoff, and an on-disk response cache."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import __version__

USER_AGENT = f"psychsearch/{__version__} (search-string testing tool)"
RETRY_STATUSES = {500, 502, 503, 504}
MAX_ATTEMPTS = 4
MAX_RETRY_AFTER = 15  # seconds; a longer Retry-After means a quota, not a blip


class HttpError(Exception):
    def __init__(self, status: int | None, message: str, headers: httpx.Headers | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.headers = headers
        self.body = body


class Http:
    def __init__(self, cache_dir: Path | None, ttl: float = 7 * 86400):
        self.cache_dir = cache_dir
        self.ttl = ttl
        self.client = httpx.Client(timeout=45, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        self._last_request: dict[str, float] = {}
        self.requests_made = 0
        self.cache_hits = 0

    def _cache_path(self, url: str, params: dict, secret: tuple[str, ...], extra) -> Path | None:
        if self.cache_dir is None:
            return None
        visible = sorted((k, str(v)) for k, v in params.items() if k not in secret)
        digest = hashlib.sha256(json.dumps([url, visible, extra], sort_keys=True).encode()).hexdigest()
        return self.cache_dir / f"{digest}.txt"

    def _throttle(self, host: str, min_interval: float) -> None:
        wait = self._last_request.get(host, 0) + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request[host] = time.monotonic()

    def get(
        self,
        url: str,
        params: dict,
        *,
        min_interval: float = 0.0,
        post: bool = False,
        secret: tuple[str, ...] = (),
        json_body: dict | None = None,
        headers: dict | None = None,
        cache: bool = True,
        cache_scope: str = "",
    ) -> tuple[str, httpx.Headers | None]:
        """Return (body, headers). Headers are None when the body came from the cache.

        `json_body` sends a JSON POST. `cache_scope` separates cached answers that depend on
        something outside the request itself (e.g. which account profile is in use).
        """
        path = self._cache_path(url, params, secret, [json_body, cache_scope]) if cache else None
        if path is not None and path.exists() and time.time() - path.stat().st_mtime < self.ttl:
            self.cache_hits += 1
            return path.read_text(encoding="utf-8"), None

        host = urlsplit(url).netloc
        failure = "no response"
        for attempt in range(MAX_ATTEMPTS):
            self._throttle(host, min_interval)
            self.requests_made += 1
            try:
                if json_body is not None:
                    response = self.client.post(url, params=params, json=json_body, headers=headers)
                elif post:
                    response = self.client.post(url, data=params, headers=headers)
                else:
                    response = self.client.get(url, params=params, headers=headers)
            except httpx.TransportError as exc:
                failure = f"{type(exc).__name__}: {exc}"
                time.sleep(2**attempt)
                continue
            status = response.status_code
            if status == 429:
                retry_after = float(response.headers.get("retry-after") or 2**attempt)
                if retry_after > MAX_RETRY_AFTER:
                    raise HttpError(status, response.text[:400], response.headers, response.text)
                failure = "HTTP 429 (rate limited)"
                time.sleep(retry_after)
                continue
            if status in RETRY_STATUSES:
                failure = f"HTTP {status}"
                time.sleep(2**attempt)
                continue
            if status >= 400:
                raise HttpError(status, f"HTTP {status}: {response.text[:400]}", response.headers, response.text)
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(response.text, encoding="utf-8")
            return response.text, response.headers
        raise HttpError(None, f"{host}: gave up after {MAX_ATTEMPTS} attempts ({failure})")
