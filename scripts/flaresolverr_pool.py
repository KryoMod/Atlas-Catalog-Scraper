#!/usr/bin/env python3
"""
Multi-instance FlareSolverr pool for parallel scraping.
=======================================================

Distributes requests across multiple FlareSolverr Docker containers using
round-robin, enabling true parallelism.  FlareSolverr is single-threaded
per session, so running *N* containers with *N* sessions gives *N*×
throughput for I/O-bound scraping workloads.

Each instance in the pool owns:
  - A unique FlareSolverr URL (different host/port → different container)
  - A persistent Chrome session (cookies survive across requests)
  - Its own failure / request counters for observability

Usage::

    from flaresolverr_pool import FlareSolverrPool, parse_flaresolverr_urls

    urls = parse_flaresolverr_urls()          # reads FLARESOLVERR_URLS env var
    pool = FlareSolverrPool(urls, verbose=True)
    resp = pool.get("https://dlpsgame.com/category/ps5/", wait_seconds=8)
    print(resp.status_code, len(resp.text))
    pool.destroy_all()

The response object (``CurlResponse``) is API-compatible with the one defined
in ``scrape_dlpsgame.py`` (``.status_code``, ``.text``, ``.url``), so the
pool can be used as a drop-in replacement for the single-instance helpers.

Thread-safety: the pool is safe for use with
``concurrent.futures.ThreadPoolExecutor`` — round-robin selection is
protected by a ``threading.Lock``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Logging — reuse the same logger name as scrape_dlpsgame.py so that
# --verbose / logging.basicConfig() configuration is shared.
# ---------------------------------------------------------------------------

log = logging.getLogger("dlpsgame-scraper")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default warm-up URL — resolves the Cloudflare challenge and stores cookies.
WARMUP_URL = "https://dlpsgame.com/category/ps5/"

#: Default maxTimeout (ms) sent to FlareSolverr's request.get command.
DEFAULT_MAX_TIMEOUT = 90_000  # 90 s

#: Extra seconds added to the HTTP-level timeout to account for CF challenge
#: resolution time + optional waitInSeconds rendering delay.
_TIMEOUT_PADDING = 30


# ---------------------------------------------------------------------------
# CurlResponse — compatible with scrape_dlpsgame.py
# ---------------------------------------------------------------------------

@dataclass
class CurlResponse:
    """Lightweight response wrapper compatible with ``scrape_dlpsgame.CurlResponse``.

    Attributes:
        status_code: HTTP status code (or 0 if unknown).
        text:        HTML body returned by FlareSolverr.
        url:         Final URL after any redirects.
    """

    status_code: int
    text: str
    url: str


# ---------------------------------------------------------------------------
# Internal: single FlareSolverr instance descriptor
# ---------------------------------------------------------------------------

@dataclass
class _Instance:
    """Tracks the state of one FlareSolverr container + session."""

    url: str
    session_id: str
    ready: bool = False
    failures: int = 0
    requests: int = 0


# ---------------------------------------------------------------------------
# FlareSolverrPool
# ---------------------------------------------------------------------------

class FlareSolverrPool:
    """Round-robin pool of FlareSolverr instances for parallel scraping.

    Each instance has its own Chrome session with valid Cloudflare cookies.
    Requests are distributed across instances using round-robin, enabling
    parallel scraping without FlareSolverr's single-session bottleneck.

    Usage::

        pool = FlareSolverrPool([
            "http://localhost:8191/v1",
            "http://localhost:8192/v1",
            "http://localhost:8193/v1",
        ])
        resp = pool.get("https://dlpsgame.com/category/ps5/", wait_seconds=8)
        pool.destroy_all()
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, urls: list[str], *, verbose: bool = False) -> None:
        """Initialize pool with FlareSolverr URLs.

        For each URL:
        1. Create a persistent session via ``sessions.create``.
        2. Warm up the session by requesting the PS5 category page
           (resolves the Cloudflare challenge and stores cookies).
        3. Mark the instance as *ready* for use.

        If an individual instance fails to initialize it is skipped (not
        added to the active pool) and a warning is logged.  The pool can
        still operate with the remaining healthy instances.

        Args:
            urls:    List of FlareSolverr endpoint URLs
                     (e.g. ``["http://localhost:8191/v1"]``).
            verbose: If *True*, emit DEBUG-level messages during init.
        """
        if not urls:
            raise ValueError("At least one FlareSolverr URL is required")

        self._verbose = verbose
        self._instances: list[_Instance] = []
        self._rr_index: int = 0
        self._lock = threading.Lock()
        self._total_requests: int = 0

        timestamp = int(time.time())

        for idx, url in enumerate(urls):
            session_id = f"pool-{timestamp}-{idx}"
            inst = _Instance(url=url, session_id=session_id)

            log.info(
                "Pool init [%d/%d] %s  session=%s",
                idx + 1, len(urls), url, session_id,
            )

            try:
                # 1) Create the session
                self._post(url, {
                    "cmd": "sessions.create",
                    "session": session_id,
                })
                log.debug("  session created on %s", url)

                # 2) Warm up — resolve CF challenge & store cookies
                resp = self._request_get(
                    inst,
                    WARMUP_URL,
                    max_timeout=60_000,
                )
                log.info(
                    "  ✓ warm-up OK (HTTP %d, %d bytes) on %s",
                    resp.status_code, len(resp.text), url,
                )

                inst.ready = True

            except Exception as exc:
                log.warning(
                    "  ⚠ Failed to initialize %s: %s — skipping",
                    url, exc,
                )
                # Attempt to destroy the partially-created session so we
                # don't leak a Chrome process inside the container.
                try:
                    self._post(url, {
                        "cmd": "sessions.destroy",
                        "session": session_id,
                    })
                except Exception:
                    pass
                continue

            self._instances.append(inst)

        if not self._instances:
            raise RuntimeError(
                "No FlareSolverr instances could be initialized. "
                "Check that your containers are running and reachable."
            )

        log.info(
            "FlareSolverrPool ready — %d/%d instances active",
            len(self._instances), len(urls),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        max_timeout: int = DEFAULT_MAX_TIMEOUT,
        wait_seconds: int | None = None,
    ) -> CurlResponse:
        """Send a GET request through the pool using round-robin.

        Selects the next available instance, sends the request via its
        session, and returns a ``CurlResponse`` compatible with the
        existing scraper.

        On failure the instance's failure counter is incremented and the
        next instance is tried, up to *N* attempts (where *N* is the
        pool size).  If all attempts fail, a ``RuntimeError`` is raised.

        Args:
            url:           The URL to fetch.
            max_timeout:   ``maxTimeout`` (ms) passed to FlareSolverr.
            wait_seconds:  Optional ``waitInSeconds`` — delay after CF
                           challenge resolution, before HTML capture.
                           Useful for JS-rendered content.

        Returns:
            A ``CurlResponse`` with ``.status_code``, ``.text``, ``.url``.

        Raises:
            RuntimeError: If all instances fail to serve the request.
        """
        n = len(self._instances)
        if n == 0:
            raise RuntimeError("Pool has no active instances")

        last_exc: Exception | None = None

        with self._lock:
            start_idx = self._rr_index
            self._rr_index = (self._rr_index + 1) % n

        for attempt in range(n):
            idx = (start_idx + attempt) % n
            inst = self._instances[idx]

            try:
                resp = self._request_get(
                    inst, url,
                    max_timeout=max_timeout,
                    wait_seconds=wait_seconds,
                )
                with self._lock:
                    inst.requests += 1
                    self._total_requests += 1
                return resp

            except Exception as exc:
                last_exc = exc
                with self._lock:
                    inst.failures += 1
                log.warning(
                    "  ⚠ Instance %s failed for %s: %s  (attempt %d/%d)",
                    inst.url, url, exc, attempt + 1, n,
                )

        raise RuntimeError(
            f"All {n} instances failed for {url}"
        ) from last_exc

    def destroy_all(self) -> None:
        """Destroy all sessions across all instances.

        Properly closes Chrome instances to free Docker-container resources.
        Should be called when scraping is complete.
        """
        for inst in self._instances:
            try:
                self._post(inst.url, {
                    "cmd": "sessions.destroy",
                    "session": inst.session_id,
                })
                log.info("  ✓ Session destroyed: %s on %s", inst.session_id, inst.url)
            except Exception as exc:
                log.warning(
                    "  ⚠ Failed to destroy session %s on %s: %s",
                    inst.session_id, inst.url, exc,
                )
            inst.ready = False

        log.info("FlareSolverrPool — all sessions destroyed")

    def get_stats(self) -> dict[str, Any]:
        """Return pool statistics.

        Returns:
            A dict with the following keys:

            - ``total_instances``      — total instances in the pool
            - ``ready_instances``      — instances currently marked ready
            - ``total_requests``       — cumulative requests served
            - ``requests_per_instance``— per-instance request counts
            - ``total_failures``       — cumulative failures
            - ``failures_per_instance``— per-instance failure counts
        """
        with self._lock:
            return {
                "total_instances": len(self._instances),
                "ready_instances": sum(1 for i in self._instances if i.ready),
                "total_requests": self._total_requests,
                "requests_per_instance": [i.requests for i in self._instances],
                "total_failures": sum(i.failures for i in self._instances),
                "failures_per_instance": [i.failures for i in self._instances],
            }

    @property
    def size(self) -> int:
        """Number of active instances in the pool."""
        return len(self._instances)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _post(url: str, payload: dict, *, timeout: int = 120) -> dict:
        """Send a JSON POST to a FlareSolverr instance and return the parsed response.

        Args:
            url:     FlareSolverr endpoint (e.g. ``http://localhost:8191/v1``).
            payload: JSON-serialisable command dict.
            timeout: HTTP-level timeout in seconds.

        Returns:
            Parsed JSON response dict.

        Raises:
            RuntimeError: If the instance is unreachable.
        """
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"FlareSolverr unreachable at {url}. "
                f"Is the container running? Detail: {exc}"
            ) from exc

    def _request_get(
        self,
        inst: _Instance,
        url: str,
        *,
        max_timeout: int = DEFAULT_MAX_TIMEOUT,
        wait_seconds: int | None = None,
    ) -> CurlResponse:
        """Issue a ``request.get`` command via a specific instance's session.

        Args:
            inst:          The pool instance to use.
            url:           Target URL.
            max_timeout:   FlareSolverr ``maxTimeout`` (ms).
            wait_seconds:  Optional ``waitInSeconds`` for JS rendering.

        Returns:
            A ``CurlResponse`` with ``.status_code``, ``.text``, ``.url``.

        Raises:
            RuntimeError: If FlareSolverr returns an error or fails to
                          resolve the Cloudflare challenge.
        """
        payload: dict[str, Any] = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": max_timeout,
            "session": inst.session_id,
        }
        if wait_seconds and wait_seconds > 0:
            payload["waitInSeconds"] = wait_seconds

        # HTTP timeout must cover CF challenge resolution + optional
        # waitInSeconds rendering delay + a safety padding.
        post_timeout = (max_timeout // 1000) + _TIMEOUT_PADDING + (wait_seconds or 0)

        data = self._post(inst.url, payload, timeout=post_timeout)

        if data.get("status") != "ok":
            raise RuntimeError(
                f"FlareSolverr error: {data.get('message', 'unknown error')}"
            )

        solution = data.get("solution", {})
        status = solution.get("status", 200)
        html = solution.get("response", "")
        final_url = solution.get("url", url)

        # Safety check: if the returned HTML is still a Cloudflare
        # challenge page, the resolution failed.
        if html and ("Just a moment" in html[:500] or "challenge-platform" in html[:2000]):
            raise RuntimeError(
                f"FlareSolverr could not resolve Cloudflare challenge for {url}"
            )

        return CurlResponse(status_code=status, text=html, url=final_url)


# ---------------------------------------------------------------------------
# Environment-variable helper
# ---------------------------------------------------------------------------

def parse_flaresolverr_urls(env_var: str = "FLARESOLVERR_URLS") -> list[str]:
    """Parse FlareSolverr URLs from an environment variable.

    Format: comma-separated URLs.

    Example::

        FLARESOLVERR_URLS=http://localhost:8191/v1,http://localhost:8192/v1,http://localhost:8193/v1

    Fallback order:

    1. ``FLARESOLVERR_URLS`` (comma-separated list of URLs).
    2. ``FLARESOLVERR_URL``  (single URL — existing behaviour).
    3. ``["http://localhost:8191/v1"]`` (default single instance).

    Args:
        env_var: Name of the environment variable holding the
                 comma-separated URL list.  Defaults to
                 ``"FLARESOLVERR_URLS"``.

    Returns:
        A list of FlareSolverr endpoint URLs (stripped of whitespace).
    """
    # 1) Try the multi-URL variable
    raw = os.environ.get(env_var, "").strip()
    if raw:
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        if urls:
            log.debug("Parsed %d FlareSolverr URLs from %s", len(urls), env_var)
            return urls

    # 2) Fall back to the single-URL variable used by scrape_dlpsgame.py
    single = os.environ.get("FLARESOLVERR_URL", "").strip()
    if single:
        log.debug("Using single FLARESOLVERR_URL=%s", single)
        return [single]

    # 3) Default
    default = "http://localhost:8191/v1"
    log.debug("No FlareSolverr env vars set — defaulting to %s", default)
    return [default]
