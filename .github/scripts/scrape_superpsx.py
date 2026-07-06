#!/usr/bin/env python3
"""
Scraper SuperPSX - PS5 Games Catalog
-------------------------------------
Crawls https://www.superpsx.com/category/ps5/ps5-games/ (pagination /page/N/),
visits each game page to extract metadata from the info table, then visits each
DLL page (/dll-*) to extract download links, firmware requirements, and format
tags. Produces a JSON catalog in the SAME format as dlpsgame-ps5.json.

superpsx.com is now behind Cloudflare: a simple `curl` request receives an
HTTP 403 "Just a moment" response. The FlareSolverr backend (`--http-backend
flaresolverr`) launches a real Chrome instance to bypass the challenge; this
is the mode to use in CI environments or when using datacenter IPs.

HTML structure (SuperPSX-specific):
  - Category page: game links found in `.entry-title a` (numeric slug, e.g.,
    `/26528-2626/`); pagination at `/category/ps5/ps5-games/page/N/`
  - Game page: info table (Game Name, Platform, Genre, Mode, Release Date,
    Version); link to the download page with a slug starting with "dll-"
    (e.g., `/dll-cvps5/`)
  - DLL page: table containing a row "Game (vXX.XXX) ⇛ MultiHost" that points
    to a `keepshield.org/safe/<id>` link (a link-locker aggregating mirrors)
  - keepshield: retrieved via FlareSolverr (JS rendering); exposes the actual
    mirrors (vikingfile, 1fichier, mega, etc.) — see `resolve_keepshield()`

Usage:
    # Full scrape (all pages)
    python scrape_superpsx.py

    # Limited scrape for testing
    python scrape_superpsx.py --max-pages 2 --max-games 10

    # Custom output path
    python scrape_superpsx.py --out superpsx-ps5.json

    # Verbose mode
    python scrape_superpsx.py --verbose

    # With concurrency and custom delay
    python scrape_superpsx.py --concurrency 2 --delay 0.5
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from formats import detect_formats, normalize_formats
from sizes import extract_size, parse_size_bytes

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://www.superpsx.com"
PS5_CATEGORY_URL = f"{BASE_URL}/category/ps5/ps5-games/"
SITE_SOURCE = "superpsx.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 30       # seconds per curl request
PAGE_DELAY = 1.0           # delay between requests (seconds)
HTTP_RETRIES = 5           # retries on 429/503/timeout

# FlareSolverr configuration (placeholder for future use)
FS_REQUEST_TIMEOUT = 90
FS_MAX_TIMEOUT = 90000
FS_WAIT_SECONDS = 3  # attente JS keepshield ; miroirs injectés vite (cf. sonde). Repli lien brut si 0.

# Disk cache for resuming after interruption
DISK_CACHE_DIR = Path(".scrape_cache_superpsx")
DISK_CACHE_ENABLED = True

# curl binary path
CURL_BIN = shutil.which("curl") or "curl"

# HTTP backend: "curl" (default) or "flaresolverr"
# superpsx.com has switched to Cloudflare (simple curl → 403 "Just a moment").
# The flaresolverr backend launches a true Chrome that bypasses
_HTTP_BACKEND: str = "curl"

FLARESOLVERR_URL = "http://localhost:8191/v1"

_FS_SESSION_ID: str | None = None

# Multi-instance FlareSolverr pool (round-robin across N Docker containers).
# Populated by init_flaresolverr_session() when FLARESOLVERR_URLS (CSV)
# contains multiple URLs. With a single URL, a single session is maintained.
_FS_POOL = None  # type: "FlareSolverrPool | None"

# Hosts from "legitimate" hosting providers recognized on KeepShield (link-locker) pages.
# Anything not on this list is considered ads/junk and filtered out.
KEEPSHIELD_MIRROR_HOSTS = (
    "vikingfile",
    "1fichier",
    "mega.nz",
    "mega.co.nz",
    "gofile",
    "akirabox",
    "pixeldrain",
    "buzzheavier",
    "datanodes",
    "fileaxa",
    "rapidgator",
    "1cloudfile",
    "mediafire",
    "datavaults",
    "filekeeper",
)

# Ad/junk/analytics domains to be systematically excluded from resolution
# keepshield (they appear in the rendered DOM but are not mirrors).
KEEPSHIELD_JUNK_HOSTS = (
    "avouchlawsrethink.com",
    "cdn.jsdelivr.net",
    "jsdelivr.net",
    "googletagmanager.com",
    "google-analytics.com",
    "analytics.google.com",
    "schema.org",
    "w3.org",
    "gmpg.org",
    "facebook.com",
    "facebook.net",
    "twitter.com",
    "x.com",
    "youtube.com",
    "youtu.be",
    "discord.gg",
    "discord.com",
    "t.me",
    "telegram.org",
    "keepshield.org",
    "gstatic.com",
    "googleapis.com",
    "cloudflare.com",
    "doubleclick.net",
)

# Cache keepshield resolutions → mirror list (avoids re-resolving
# the same /safe/<id> multiple times within a single run).
_KEEPSHIELD_CACHE: dict[str, list[dict[str, str]]] = {}

# ---------------------------------------------------------------------------
# Mirror name mapping (consistent with dlpsgame scraper)
# ---------------------------------------------------------------------------

MIRROR_PATTERNS = [
    # (pattern in URL, display name)
    ("akirabox", "Akia"),
    ("vikingfile", "Viki"),
    ("datanodes.to", "Data"),
    ("filekeeper", "Filek"),
    ("datavaults", "Vault"),
    ("buzzheavier", "Buzz"),
    ("1fichier", "1File"),
    ("mediafire", "Mediafire"),
    ("rootz.so", "Rootz"),
    ("gofile", "Gofile"),
    ("1cloudfile", "1Cloud"),
    ("mega.nz", "Mega"),
    ("mega.co.nz", "Mega"),
]

# SuperPSX-specific host name normalization
# The site uses short host abbreviations in table text
HOST_ALIASES = {
    "akr": "Akia",
    "viki": "Viki",
    "rootz": "Rootz",
    "onefile": "1File",
    "1file": "1File",
    "gofile": "Gofile",
    "mediafire": "Mediafire",
    "mega": "Mega",
    "filek": "Filek",
    "data": "Data",
    "buzz": "Buzz",
    "vault": "Vault",
}

# Non-host domains to exclude from download links
NON_HOST_HOSTS = {
    "superpsx.com",
    "www.superpsx.com",
    "www.google.com",
    "google.com",
    "www.facebook.com",
    "facebook.com",
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
    "discord.gg",
    "discord.com",
    "www.discord.com",
    "t.me",
    "reddit.com",
    "www.reddit.com",
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "wikipedia.org",
}

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

PPSA_RE = re.compile(r"\b(PPSA\d{5})\b", re.I)
# Matches version like "v01.024", "v1.002", "01.024" (after PPSA in version row)
VERSION_RE = re.compile(r"v(?:ersion)?\s*0?(\d+\.\d+(?:\.\d+)?)", re.I)
# Matches version in DLL page Version row: "PPSA02182 – USA" (no v prefix sometimes)
VERSION_FROM_ROW_RE = re.compile(r"0?(\d+\.\d{3,})", re.I)
# Size detection is centralized in sizes.py (fixes the "to" bug).
# DLL page link on game page
DLL_LINK_RE = re.compile(r'href="(https://(?:www\.)?superpsx\.com/dll-[^/"]+/)"')
# Separator character used in DLL page tables
SEPARATOR = "\u21db"  # ⇛
# Download link selector pattern on DLL pages
DLL_DOWNLOAD_A_RE = re.compile(r"external", re.I)
# Game format tags: [APR-EMU], [FPKG], etc.
FORMAT_TAG_RE = re.compile(r"\[([A-Z0-9\-]+)\]")
# Firmware compatibility from Note rows: "Working 10.xx – 5.xx"
FW_COMPAT_RE = re.compile(
    r"(?:Working|Works?)\s+(\d+\.(?:\d+|xx))\s*[–\-\u2013]\s*(\d+\.(?:\d+|xx))", re.I
)
# Firmware from Fix/Backport rows: "Fix 4.xx", "Backport 7.xx"
FW_FIX_RE = re.compile(r"(?:Fix|Backport)\s+(\d+\.(?:\d+|xx))", re.I)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("superpsx-scraper")


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# HTTP helpers (mirrors scrape_dlpsgame.py patterns)
# ---------------------------------------------------------------------------


class CurlResponse:
    """Lightweight wrapper mimicking requests.Response."""

    def __init__(self, status_code: int, text: str, final_url: str):
        self.status_code = status_code
        self.text = text
        self.url = final_url

def _curl_get(
    url: str,
    *,
    follow_redirects: bool = True,
    max_time: int | None = None,
) -> CurlResponse:
    """Execute curl as subprocess and return (status, body, final_url).

    curl is used because its native TLS fingerprint (OpenSSL) passes
    Cloudflare JA3 checks when the IP is not blocklisted."""
    timeout = max_time or REQUEST_TIMEOUT
    cmd = [
        CURL_BIN,
        "--silent", "--show-error",
        "--compressed",
        "--max-time", str(timeout),
        "--connect-timeout", "15",
        "-A", USER_AGENT,
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
        "-H", "Cache-Control: no-cache",
        "-w", "\n__CURL_META__\n%{http_code}\n%{url_effective}",
    ]
    if follow_redirects:
        cmd += ["--location", "--max-redirs", "10"]
    cmd.append(url)

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
    out = proc.stdout or ""
    err = proc.stderr or ""

    body = out
    status = 0
    final_url = url
    marker = "\n__CURL_META__\n"
    if marker in out:
        body, meta = out.split(marker, 1)
        meta_lines = meta.strip().split("\n")
        if len(meta_lines) >= 1:
            try:
                status = int(meta_lines[0])
            except ValueError:
                status = 0
        if len(meta_lines) >= 2:
            final_url = meta_lines[1]

    if proc.returncode != 0 and not out:
        raise RuntimeError(f"curl failed (code {proc.returncode}): {err.strip()}")

    return CurlResponse(
        status_code=status or proc.returncode,
        text=body,
        final_url=final_url,
    )


def _cache_key(url: str) -> str:
    """Generate a safe cache filename from a URL."""
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    slug = re.sub(r"[^a-z0-9]+", "_", url.lower())[-60:]
    return f"{slug}_{h}.html"


def _cache_get(url: str) -> str | None:
    """Retrieve cached HTML for this URL, or None."""
    if not DISK_CACHE_ENABLED:
        return None
    cache_file = DISK_CACHE_DIR / _cache_key(url)
    if cache_file.exists():
        try:
            return cache_file.read_text(encoding="utf-8")
        except Exception:
            return None
    return None


def _cache_set(url: str, html: str) -> None:
    """Store HTML in disk cache."""
    if not DISK_CACHE_ENABLED:
        return
    try:
        DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = DISK_CACHE_DIR / _cache_key(url)
        cache_file.write_text(html, encoding="utf-8")
    except Exception as exc:
        log.debug("  cache write failed for %s: %s", url, exc)


def _flaresolverr_post(payload: dict, *, timeout: int = 120) -> dict:
    import urllib.error
    import urllib.request

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        FLARESOLVERR_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"FlareSolverr unreachable on {FLARESOLVERR_URL}. "
            f"Launch it : docker run -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest. "
            f"Détail : {exc}"
        ) from exc


def _flaresolverr_get(
    url: str,
    *,
    max_time: int | None = None,
    wait_seconds: int | None = None,
) -> CurlResponse:
    """GET via FlareSolverr (vrai Chrome → franchit le challenge Cloudflare).

    Uses the multi-instance pool if active; otherwise, uses the single persistent session.
    Returns an API-compatible CurlResponse (.status_code/.text/.url).

    `wait_seconds`: delay (in seconds) allowed for the page's JS AFTER the
    challenge is resolved and BEFORE the HTML is captured (via `waitInSeconds`). Essential
    for KeepShield pages where mirrors are injected via JavaScript.."""
    timeout = max_time or FS_REQUEST_TIMEOUT

    if _FS_POOL is not None:
        return _FS_POOL.get(
            url,
            max_timeout=FS_MAX_TIMEOUT,
            wait_seconds=wait_seconds,
        )

    payload: dict = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": FS_MAX_TIMEOUT,
    }
    if _FS_SESSION_ID:
        payload["session"] = _FS_SESSION_ID
    if wait_seconds and wait_seconds > 0:
        payload["waitInSeconds"] = wait_seconds

    post_timeout = timeout + 30 + (wait_seconds or 0)
    data = _flaresolverr_post(payload, timeout=post_timeout)

    if data.get("status") != "ok":
        raise RuntimeError(
            f"FlareSolverr failed: {data.get('message', 'unknown error')}"
        )

    solution = data.get("solution", {})
    status = solution.get("status", 200)
    html = solution.get("response", "")
    final_url = solution.get("url", url)

    if html and ("Just a moment" in html[:500] or "challenge-platform" in html[:2000]):
        raise RuntimeError(
            f"FlareSolverr failed to solve the Cloudflare challenge for {url}"
        )

    return CurlResponse(status_code=status, text=html, final_url=final_url)


def init_flaresolverr_session() -> None:
    """Creates a persistent FlareSolverr session (or a multi-instance pool).

    With FLARESOLVERR_URLS (CSV of ≥ 2 URLs): sets up a round-robin pool.
    Otherwise: single persistent session (Chrome remains open with Cloudflare
    cookies, avoiding the need to relaunch the browser for every request).."""
    global _FS_SESSION_ID, _FS_POOL
    if _HTTP_BACKEND != "flaresolverr":
        return

    # Détection multi-instances : FLARESOLVERR_URLS (CSV) priority.
    try:
        from flaresolverr_pool import FlareSolverrPool, parse_flaresolverr_urls
        fs_urls = parse_flaresolverr_urls()
    except Exception as exc:
        log.debug("  pool FlareSolverr unavailable (%s) — single session", exc)
        fs_urls = []

    if len(fs_urls) > 1:
        log.info("Creation pool FlareSolverr multi-instances (%d URLs)...", len(fs_urls))
        try:
            _FS_POOL = FlareSolverrPool(fs_urls, verbose=False)
            log.info("  ✓ Pool FlareSolverr prêt : %d instance(s) active(s)", _FS_POOL.size)
            return
        except Exception as exc:
            log.warning("  ⚠ Pool initialization failed (%s) — falling back to single session", exc)
            _FS_POOL = None

    session_id = f"superpsx-{int(time.time())}"
    log.info("Creation session FlareSolverr persistent...")
    try:
        data = _flaresolverr_post({"cmd": "sessions.create", "session": session_id})
        if data.get("status") == "ok":
            _FS_SESSION_ID = session_id
            log.info("  ✓ FlareSolverr session created: %s", _FS_SESSION_ID)
            # Pre-warming: solves the Cloudflare challenge and stores cookies.
            log.info("  Pre-loading category page...")
            resp = _flaresolverr_get(PS5_CATEGORY_URL, max_time=60)
            log.info("  ✓ Pre-load OK (HTTP %d, %d bytes)",
                     resp.status_code, len(resp.text))
        else:
            log.warning("  ⚠ Session not created: %s — independent requests",
                        data.get("message"))
    except Exception as exc:
        log.warning("  ⚠ Could not create session: %s", exc)
        log.warning("    Requests will be independent (slower)")


def destroy_flaresolverr_session() -> None:
    """Destroys the FlareSolverr session (or pool) at the end of the scrape."""
    global _FS_SESSION_ID, _FS_POOL
    if _FS_POOL is not None:
        try:
            _FS_POOL.destroy_all()
        except Exception:
            pass
        _FS_POOL = None
        return
    if not _FS_SESSION_ID:
        return
    try:
        _flaresolverr_post({"cmd": "sessions.destroy", "session": _FS_SESSION_ID})
        log.info("  ✓ FlareSolverr session destroyed: %s", _FS_SESSION_ID)
    except Exception:
        pass
    _FS_SESSION_ID = None


def _fetch(url: str, *, follow_redirects: bool = True) -> CurlResponse:
    """Dispatch to the configured HTTP backend."""
    if _HTTP_BACKEND == "flaresolverr":
        return _flaresolverr_get(url)
    else:
        return _curl_get(url, follow_redirects=follow_redirects)


def http_get(
    url: str,
    *,
    follow_redirects: bool = True,
) -> CurlResponse:
    """GET with retry and exponential backoff on 429/503.

    Backoff: 5s → 15s → 30s → 60s → 120s."""
    last_exc: Exception | None = None
    backoff_table = [5, 15, 30, 60, 120]
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            resp = _fetch(url, follow_redirects=follow_redirects)
            if resp.status_code in (403, 429, 503):
                wait = backoff_table[min(attempt - 1, len(backoff_table) - 1)]
                log.warning(
                    "  HTTP %s on %s — pause %ds (attempt %d/%d)",
                    resp.status_code, url, wait, attempt, HTTP_RETRIES,
                )
                time.sleep(wait)
                continue
            return resp
        except Exception as exc:
            last_exc = exc
            wait = backoff_table[min(attempt - 1, len(backoff_table) - 1)]
            log.warning(
                "  attempt %d/%d failed (%s): %s",
                attempt, HTTP_RETRIES, url, exc,
            )
            time.sleep(wait)
    raise RuntimeError(f"HTTP failed after {HTTP_RETRIES} attempts: {url}") from last_exc


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def get_hostname(url: str) -> str:
    """Extract lowercase hostname (without port) from URL."""
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def is_non_host_url(url: str) -> bool:
    """True if URL points to a non-host domain (social, etc.)."""
    host = get_hostname(url)
    if not host:
        return True
    return host in NON_HOST_HOSTS


# parse_size_bytes (token de table « Size ») et extract_size (texte libre des
# notes) sont fournis par le module centralisé sizes.py (corrige le bug « to »).


def normalize_version(raw: str | None) -> str:
    """Normalize version string to '01.024' format.

    Handles: 'v01.024', '01.024', '1.024', '1.02', etc."""
    if not raw:
        return "01.000"
    m = VERSION_RE.search(raw)
    if m:
        parts = m.group(1).split(".")
        major = int(parts[0])
        minor = parts[1] if len(parts) > 1 else "000"
        # Pad minor to at least 3 digits
        minor = minor.ljust(3, "0")[:3]
        if len(parts) >= 3:
            patch = parts[2].ljust(3, "0")[:3]
            return f"{major:02d}.{minor}.{patch}"
        return f"{major:02d}.{minor}"
    # Try matching just a number like "01.024" without the v prefix
    m2 = VERSION_FROM_ROW_RE.search(raw)
    if m2:
        parts = m2.group(1).split(".")
        major = int(parts[0])
        minor = parts[1] if len(parts) > 1 else "000"
        minor = minor.ljust(3, "0")[:3]
        return f"{major:02d}.{minor}"
    return "01.000"


def extract_mirror_name(url: str, link_text: str = "") -> str:
    """Detect mirror name from URL (priority) or link text.

    Uses the MIRROR_PATTERNS list for URL matching and HOST_ALIASES
    for SuperPSX-specific abbreviations found in link text."""
    url_lower = url.lower()
    for pattern, name in MIRROR_PATTERNS:
        if pattern in url_lower:
            return name

    # Check link text against host aliases
    txt_lower = (link_text or "").strip().lower()
    if txt_lower in HOST_ALIASES:
        return HOST_ALIASES[txt_lower]

    # Fallback: capitalize first letter of link text
    txt = (link_text or "").strip()
    return txt.capitalize() if txt else "Mirror"


def detect_group(label_text: str) -> str:
    """Detect download group from a row label.

    Groups: Standard, Backport, DLC, Fix, Dump, exFAT"""
    t = label_text.lower()
    if "exfat" in t:
        return "exFAT"
    if "backport" in t:
        return "Backport"
    if "update" in t:
        return "Update"
    if "dlc" in t:
        return "DLC"
    if "fix" in t:
        return "Fix"
    if "game" in t:
        return "Game"
    return "Game"


# ---------------------------------------------------------------------------
# Phase 1: Discover game URLs from category pages
# ---------------------------------------------------------------------------


def _is_game_url(href: str) -> bool:
    """True if `href` looks like a valid SuperPSX game page.

    The new site uses arbitrary slugs (often numeric, e.g.,
    /26528-2626/) without the -ps5 suffix. We accept any internal slug EXCEPT:
    /category/, /page/, /tag/, /spxguide/, slugs starting with "dll-",
    emulator/guide pages, and links outside superpsx.com."""
    if not href.startswith(BASE_URL):
        return False

    host = get_hostname(href)
    if host not in ("superpsx.com", "www.superpsx.com"):
        return False

    path = urlparse(href).path.strip("/")
    if not path:
        return False

    lowered = path.lower()
    excluded_prefixes = ("category/", "page/", "tag/", "spxguide/", "author/", "dll-")
    if any(lowered.startswith(pref) for pref in excluded_prefixes):
        return False
    if "/page/" in f"/{lowered}/" or "/category/" in f"/{lowered}/":
        return False
    if "/dll-" in f"/{lowered}":
        return False

    segments = [s for s in path.split("/") if s]
    if len(segments) != 1:
        return False

    return True


def discover_game_urls(max_pages: int | None) -> list[str]:
    """Crawl all category pages and collect game page URLs.

    WordPress pagination: /category/ps5/ps5-games/ (page 1), then
    /category/ps5/ps5-games/page/2/, ... up to ~/page/25/.
    Game links are found in `.entry-title a` (WordPress selector).
    Arbitrary slugs are accepted (e.g., numeric ones like /26528-2626/), but
    category/page/tag/spxguide/dll- are excluded (see _is_game_url).
    Pagination stops as soon as a page returns NO new game links
    (or a 404), and respects max_pages.."""
    game_urls: list[str] = []
    seen: set[str] = set()

    page = 1
    while True:
        if max_pages is not None and page > max_pages:
            break

        if page == 1:
            url = PS5_CATEGORY_URL
        else:
            url = f"{PS5_CATEGORY_URL}page/{page}/"

        log.info("Discovering page %d: %s", page, url)
        try:
            resp = http_get(url)
        except Exception as exc:
            log.error("  page %d unreachable: %s — stopping pagination", page, exc)
            break

        if resp.status_code != 200:
            log.info("  page %d → HTTP %d — end of pagination", page, resp.status_code)
            break

        soup = BeautifulSoup(resp.text, "html.parser")

        anchors = soup.select(".entry-title a[href]")

        if not anchors:
            anchors = soup.select("h2.penci-entry-title a[href], article.item.hentry a[href]")
        if not anchors:
            anchors = soup.find_all("a", href=True)

        page_links: list[str] = []
        for a in anchors:
            href = (a.get("href") or "").strip()
            if not href:
                continue
            if not href.startswith("http"):
                href = urljoin(url, href)
            if _is_game_url(href) and href not in seen:
                seen.add(href)
                page_links.append(href)
                game_urls.append(href)

        if not page_links:
            log.info("  page %d: 0 new game links found — end of pagination", page)
            break

        log.info("  page %d: %d games", page, len(page_links))

        page += 1
        time.sleep(PAGE_DELAY)

    log.info("Total: %d games discovered across %d pages", len(game_urls), page - 1)
    return game_urls


# ---------------------------------------------------------------------------
# Phase 2: Parse game page (extract metadata + DLL page link)
# ---------------------------------------------------------------------------

def extract_poster_url(soup: BeautifulSoup, page_url: str) -> str | None:
    """Extract the cover/poster image URL.

    Priority:
      1. og:image / og:image:secure_url meta tags
      2. a.penci-image-holder[data-bgset]
      3. First <img> in the article body
    """
    # OpenGraph image
    for prop in ("og:image", "og:image:secure_url"):
        og = soup.find("meta", property=prop)
        if og and og.get("content"):
            return og["content"]

    # Penci image holder (used on category/game pages)
    holder = soup.select_one("a.penci-image-holder[data-bgset]")
    if holder:
        bgset = holder.get("data-bgset", "")
        if bgset:
            # data-bgset may contain multiple URLs separated by commas
            # Take the last (highest resolution) one
            urls = [u.strip().split()[0] for u in bgset.split(",") if u.strip()]
            if urls:
                return urls[-1]

    # First image in article
    article = soup.find("article") or soup
    img = article.find("img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            if not src.startswith("http"):
                src = urljoin(page_url, src)
            return src

    return None


def parse_info_table(soup: BeautifulSoup) -> dict[str, str]:
    """Parse the game info table on the game page.

    Extracts key/value pairs from `<td>` cells. Expected keys:
    Game Name, Platform, Genre, Mode, Release Date, Size, Version, Update.

    We first target `table.has-fixed-layout` (WordPress table); failing that, we
    scan all `<table>` elements and select the first one that resembles a
    key/value info table (at least one recognized expected key).."""
    info: dict[str, str] = {}

    expected_keys = {
        "game name", "platform", "genre", "mode", "release date",
        "size", "version", "update", "publisher", "developer",
    }

    def _parse_table(table: Tag) -> dict[str, str]:
        out: dict[str, str] = {}
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) >= 2:
                key = cells[0].get_text(strip=True).rstrip(":")
                value = cells[1].get_text(strip=True)
                if key and value:
                    out[key] = value
        return out

    table = soup.select_one("table.has-fixed-layout")
    if table:
        info = _parse_table(table)
        if info:
            return info

    for table in soup.find_all("table"):
        candidate = _parse_table(table)
        if candidate and any(k.lower() in expected_keys for k in candidate):
            return candidate

    return info


def find_dll_page_url(soup: BeautifulSoup, page_url: str) -> str | None:
    """Find the DLL page link on the game page.

    On the new site, the game page (/26528-2626/) contains a link to
    the download page where the slug starts with "dll-"
    (e.g., /dll-cvps5/). This is returned as the priority result. Failing that,
    we fall back to a link or button with text mentioning "Download"."."""

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        if not href.startswith("http"):
            href = urljoin(page_url, href)
        if "superpsx.com" not in href:
            continue
        slug = urlparse(href).path.strip("/").split("/")[0].lower()
        if slug.startswith("dll-") or "/dll-" in href:
            return href

    html_str = str(soup)
    m = DLL_LINK_RE.search(html_str)
    if m:
        return m.group(1)

    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True).lower()
        href = a["href"].strip()
        if not href:
            continue
        if "download" in text:
            if not href.startswith("http"):
                href = urljoin(page_url, href)
            if "superpsx.com" in href and _is_game_url(href):
                return href

    return None


def parse_game_page(url: str) -> dict | None:
    """Parse a game page to extract metadata and the DLL page URL.

    Returns a dict with: title, poster_url, info_table, dll_url, or None on failure.
    """
    log.info("  → Game page: %s", url)

    cached_html = _cache_get(url)
    if cached_html:
        log.debug("    (from disk cache)")
        html = cached_html
    else:
        try:
            resp = http_get(url)
        except Exception as exc:
            log.warning("    unreachable: %s", exc)
            return None
        if resp.status_code != 200:
            log.warning("    HTTP %d", resp.status_code)
            return None
        html = resp.text
        _cache_set(url, html)

    soup = BeautifulSoup(html, "html.parser")

    info = parse_info_table(soup)

    # Title: priority given to the table's "Game Name" (proper noun, e.g., "Code Violet"),
    # then h1/entry-title (suffix "PS5" removed), then og:title. We discard the
    # values ​​reduced to the numeric slug (e.g., "26528-2626", the WordPress slug).
    def _looks_like_slug(s: str) -> bool:
        return bool(re.fullmatch(r"\d+[-–]\d+", (s or "").strip()))

    title = ""
    for _k, _v in info.items():
        if _k.strip().lower() in ("game name", "name", "title") and (_v or "").strip():
            title = _v.strip()
            break
    if not title or _looks_like_slug(title):
        h1 = soup.select_one("h1.entry-title") or soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)
    if not title or _looks_like_slug(title):
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = og_title["content"]
    title = re.sub(r"\s*[-–]\s*(SuperPSX|Download).*$", "", title, flags=re.I).strip()
    title = re.sub(r"\s+PS5$", "", title, flags=re.I).strip()

    # Poster URL
    poster_url = extract_poster_url(soup, url)

    # DLL page URL
    dll_url = find_dll_page_url(soup, url)

    if not dll_url:
        log.debug("    no DLL page link found on game page")
        return None

    return {
        "title": title,
        "poster_url": poster_url,
        "info": info,
        "dll_url": dll_url,
        "game_url": url,
    }


# ---------------------------------------------------------------------------
# Phase 3: Parse DLL page (extract download links, firmware info, formats)
# ---------------------------------------------------------------------------

def _split_on_separator(text: str) -> list[str]:
    """Split text on the ⇛ separator, returning (label, value) parts."""
    if SEPARATOR in text:
        parts = text.split(SEPARATOR, 1)
        return [p.strip() for p in parts]
    return [text.strip()]

def _extract_download_links_from_cell(cell: Tag) -> list[dict[str, str]]:
    """Extract download links from a table cell.

    Looks for <a> tags with data-penci-link="external" and rel="nofollow".
    Also handles text-only hosts (FileK) that appear as plain text between " – " separators.
    """
    links: list[dict[str, str]] = []

    # Method 1: <a data-penci-link="external" rel="nofollow">
    for a in cell.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        penci_link = a.get("data-penci-link", "")
        rel = a.get("rel", [])

        # Accept links that are external or have nofollow rel
        if (
            penci_link == "external"
            or "nofollow" in (rel if isinstance(rel, list) else [rel])
            or (href.startswith("http") and "superpsx.com" not in href)
        ):
            if href and not is_non_host_url(href):
                name = extract_mirror_name(href, text)
                links.append({"name": name, "url": href})

    # Method 2: Any other <a href> pointing to known host patterns
    for a in cell.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("http") and not is_non_host_url(href):
            # Check if it matches a known mirror pattern
            already_found = any(l["url"] == href for l in links)
            if not already_found:
                name = extract_mirror_name(href, a.get_text(strip=True))
                links.append({"name": name, "url": href})

    # Method 3: Text-only hosts (e.g., "FileK") that appear as plain text
    # These are separated by " – " in the cell text
    if not links:
        cell_text = cell.get_text(strip=True)
        if " – " in cell_text or " - " in cell_text:
            parts = re.split(r"\s*[–\-]\s*", cell_text)
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                part_lower = part.lower()
                # Check if this part is a known host alias
                if part_lower in HOST_ALIASES:
                    # Text-only host: we include it with empty URL
                    # (or skip it, depending on configuration)
                    links.append({"name": HOST_ALIASES[part_lower], "url": ""})

    return links


def _detect_section_label(table: Tag) -> str | None:
    """Detect if a table is a section separator.

    Separator tables have no has-fixed-layout class, contain a single <td>
    with a PPSA ID and possibly a variant label (e.g., 'exFAT')."""
    if table.get("class") and "has-fixed-layout" in " ".join(table.get("class", [])):
        return None

    cells = table.find_all("td")
    if len(cells) == 1:
        text = cells[0].get_text(strip=True)
        if PPSA_RE.search(text):
            return text
    return None


# ---------------------------------------------------------------------------
# Résolution keepshield (link-locker agrégateur de miroirs)
# ---------------------------------------------------------------------------


def is_keepshield_url(url: str) -> bool:
    """True si l'URL est un lien keepshield.org/safe/<id> (link-locker)."""
    host = get_hostname(url)
    if host not in ("keepshield.org", "www.keepshield.org"):
        return False
    return "/safe/" in urlparse(url).path


def _is_keepshield_mirror(url: str) -> bool:
    """True if the URL points to a genuine host (and not an ad or junk site).

    We require a known host (KEEPSHIELD_MIRROR_HOSTS) AND reject any host
    listed as junk/analytics (KEEPSHIELD_JUNK_HOSTS)."""
    if not url or not url.startswith("http"):
        return False
    host = get_hostname(url)
    if not host:
        return False
    # Filtre pub/junk
    if any(junk in host for junk in KEEPSHIELD_JUNK_HOSTS):
        return False
    # Doit correspondre à un hébergeur connu
    return any(m in host for m in KEEPSHIELD_MIRROR_HOSTS)


def resolve_keepshield(url: str) -> list[dict[str, str]]:
    """Resolves a keepshield.org/safe/<id> link into a list of actual mirrors.

    Keepshield is a "link-locker": the page aggregates multiple mirrors
    (vikingfile, 1fichier, mega, etc.) that are injected into the DOM
    only *after* JavaScript execution. It is therefore retrieved via
    FlareSolverr (never via simple curl), with a short rendering delay,
    after which the hrefs for known hosts are extracted while filtering
    out ads/junk.

    Returns a list of dicts {"url": ..., "mirror": <short name>} compatible
    with the downloadLinks format. Returns an EMPTY list if resolution
    fails or yields no results (in which case the caller falls back to the
    raw keepshield link).."""

    if url in _KEEPSHIELD_CACHE:
        return _KEEPSHIELD_CACHE[url]

    html = _cache_get(url)
    if not html:
        try:
            resp = _flaresolverr_get(
                url,
                max_time=FS_REQUEST_TIMEOUT,
                wait_seconds=FS_WAIT_SECONDS,
            )
        except Exception as exc:
            log.warning("    keepshield: resolution failed for %s — %s", url, exc)
            _KEEPSHIELD_CACHE[url] = []
            return []
        if resp.status_code != 200:
            log.warning("    keepshield: HTTP %d for %s", resp.status_code, url)
            _KEEPSHIELD_CACHE[url] = []
            return []
        html = resp.text
        _cache_set(url, html)

    soup = BeautifulSoup(html, "html.parser")

    seen: set[str] = set()
    mirrors: list[dict[str, str]] = []

    candidates: list[str] = [a["href"].strip() for a in soup.find_all("a", href=True)]

    for host in KEEPSHIELD_MIRROR_HOSTS:
        for m in re.finditer(
            r'https?://[^\s"\'<>]*' + re.escape(host) + r'[^\s"\'<>]*', html, re.I
        ):
            candidates.append(m.group(0))

    for href in candidates:
        if not href or not href.startswith("http"):
            continue
        if not _is_keepshield_mirror(href):
            continue

        if href in seen:
            continue
        seen.add(href)
        mirror_name = extract_mirror_name(href)
        mirrors.append({"url": href, "mirror": mirror_name})

    if mirrors:
        log.info("    keepshield resolved: %d mirror(s) for %s", len(mirrors), url)
    else:
        log.debug("    keepshield: 0 mirrors extracted for %s", url)

    _KEEPSHIELD_CACHE[url] = mirrors
    return mirrors


def parse_dll_page(url: str) -> dict | None:
    """Parse a DLL page to extract download links, firmware, and format info.

    DLL pages contain:
    - Header tables (no has-fixed-layout): host info like "(AKR, Viki, and OneFile)"
    - Data tables (has-fixed-layout): rows with ⇛ separators
    - Separator tables: single-cell tables with PPSA ID marking new sections

    Returns a dict with:
      - links: list of {name, url, group} download links
      - title_id: PPSA ID
      - version: normalized version string
      - region: region code (USA, EUR, etc.)
      - firmware: firmware requirement
      - firmware_compat: firmware compatibility range
      - password: always "SuperPSX"
      - voices: voice languages
      - screen_languages: subtitle languages
      - file_formats: list of detected format tags
      - notes: additional notes
      - sections: list of section info (for multi-section pages)
    """
    log.info("  → DLL page: %s", url)

    cached_html = _cache_get(url)
    if cached_html:
        log.debug("    (from disk cache)")
        html = cached_html
    else:
        try:
            resp = http_get(url)
        except Exception as exc:
            log.warning("    unreachable: %s", exc)
            return None
        if resp.status_code != 200:
            log.warning("    HTTP %d", resp.status_code)
            return None
        html = resp.text
        _cache_set(url, html)

    soup = BeautifulSoup(html, "html.parser")

    # Collect all tables on the page
    all_tables = soup.find_all("table")

    # Track current section (for multi-section pages)
    current_section = ""
    current_section_label = ""

    # Results
    all_links: list[dict[str, str]] = []
    title_id: str | None = None
    version: str | None = None
    region: str | None = None
    firmware: str | None = None
    firmware_compat: str | None = None
    password: str | None = None
    voices: str | None = None
    screen_languages: str | None = None
    notes: list[str] = []
    file_formats: list[str] = []
    sections: list[dict] = []

    for table in all_tables:
        table_classes = " ".join(table.get("class", []))

        # Check if this is a separator table (marks new section)
        section_label = _detect_section_label(table)
        if section_label is not None:
            current_section = section_label
            # Try to extract variant label (e.g., "exFAT")
            section_lower = section_label.lower()
            if "exfat" in section_lower:
                current_section_label = "exFAT"
            elif "backport" in section_lower:
                current_section_label = "Backport"
            else:
                current_section_label = ""

            sections.append({
                "label": section_label,
                "variant": current_section_label,
            })
            log.debug("    section: %s", section_label)
            continue

        # Only process data tables (has-fixed-layout)
        if "has-fixed-layout" not in table_classes:
            # Header table: skip (contains host info)
            continue

        # Parse rows in the data table
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            label_cell = cells[0]
            value_cell = cells[1] if len(cells) >= 2 else cells[0]

            label_text = label_cell.get_text(strip=True)
            value_text = value_cell.get_text(strip=True)

            # Check for ⇛ separator in the label or value cell.
            # SuperPSX DLL tables have two common layouts:
            #   1. <td>Version ⇛</td><td>PPSA02182 – USA</td>
            #      → ⇛ is at end of label cell, value is in second cell
            #   2. <td>Version</td><td>⇛ PPSA02182 – USA</td>
            #      → ⇛ is at start of value cell
            #   3. <td>Game (v01.024) [APR-EMU] ⇛</td><td>download links</td>
            #      → ⇛ at end of label, download links in value cell
            #
            # row_label: the key (before ⇛)
            # row_info: any text after ⇛ in the same cell (usually empty)
            # value_text: full text of the second cell
            # full_info: combined row_info + value_text for searching
            if SEPARATOR in label_text:
                parts = label_text.split(SEPARATOR, 1)
                row_label = parts[0].strip()
                row_info = parts[1].strip() if len(parts) > 1 else ""
            elif SEPARATOR in value_text:
                # Separator is in the value cell: "⇛ PPSA02182 – USA"
                row_label = label_text.rstrip(":")
                val_parts = value_text.split(SEPARATOR, 1)
                row_info = val_parts[-1].strip() if val_parts else ""
            else:
                # No separator found — treat as key/value pair
                row_label = label_text.rstrip(":")
                row_info = ""

            # full_info combines any text after ⇛ in the label cell
            # with the full value cell text — used for extracting PPSA IDs,
            # firmware, region, etc.
            full_info = f"{row_info} {value_text}".strip()

            # Process based on row label
            row_label_lower = row_label.lower()

            # Version row: "Version ⇛ PPSA02182 – USA [Thank @CREDITS]"
            # The PPSA ID and region are in the value cell (full_info)
            if row_label_lower == "version":
                # Extract title ID from full_info (combines label-after-⇛ + value cell)
                ppsa_match = PPSA_RE.search(full_info)
                if ppsa_match:
                    title_id = ppsa_match.group(1).upper()

                # Extract region (USA, EUR, ASIA, JPN, etc.)
                region_match = re.search(
                    r"[–\-]\s*([A-Z]{2,5})\b", full_info,
                )
                if region_match:
                    region = region_match.group(1)

            # Game row: "Game (vXX.XXX) [TAG] ⇛ download links"
            elif row_label_lower.startswith("game") or row_label_lower.startswith("update"):
                # Extract version from the label
                ver_match = VERSION_RE.search(row_label)
                if ver_match:
                    version = normalize_version(row_label)

                # Extract format tags [APR-EMU], [FPKG], etc.
                for tag_match in FORMAT_TAG_RE.finditer(row_label):
                    tag = tag_match.group(1)
                    if tag not in file_formats:
                        file_formats.append(tag)

                # Extract download links from value cell
                dl_links = _extract_download_links_from_cell(value_cell)
                group = detect_group(row_label)
                if current_section_label:
                    group = current_section_label

                for link in dl_links:
                    link_name = link["name"]
                    # Prefix with group name for non-Standard groups
                    if group in ("Backport", "DLC", "Fix", "Dump", "exFAT"):
                        link_name = f"{group} - {link_name}"
                    all_links.append({
                        "name": link_name,
                        "url": link["url"],
                        "group": group,
                    })

            # Fix row: "Fix X.xx (@USER) ⇛ download links"
            elif row_label_lower.startswith("fix"):
                fw_match = FW_FIX_RE.search(row_label)
                if fw_match:
                    notes.append(f"Fix {fw_match.group(1)}")

                dl_links = _extract_download_links_from_cell(value_cell)
                for link in dl_links:
                    link_name = f"Fix - {link['name']}"
                    all_links.append({
                        "name": link_name,
                        "url": link["url"],
                        "group": "Fix",
                    })

            # Backport row: "Backport X.xx (@USER) ⇛ download links"
            elif row_label_lower.startswith("backport"):
                fw_match = FW_FIX_RE.search(row_label)
                if fw_match:
                    notes.append(f"Backport {fw_match.group(1)}")

                dl_links = _extract_download_links_from_cell(value_cell)
                for link in dl_links:
                    link_name = f"Backport - {link['name']}"
                    all_links.append({
                        "name": link_name,
                        "url": link["url"],
                        "group": "Backport",
                    })

            # DLC row: "DLC (@USER) ⇛ download links"
            elif row_label_lower.startswith("dlc"):
                dl_links = _extract_download_links_from_cell(value_cell)
                for link in dl_links:
                    link_name = f"DLC - {link['name']}"
                    all_links.append({
                        "name": link_name,
                        "url": link["url"],
                        "group": "DLC",
                    })

            # FW REQUIRED row
            elif row_label_lower.startswith("fw") and "required" in row_label_lower:
                firmware = full_info.strip()

            # Note row: "Working 10.xx – 5.xx"
            elif row_label_lower == "note":
                compat_match = FW_COMPAT_RE.search(full_info)
                if compat_match:
                    firmware_compat = f"{compat_match.group(1)} – {compat_match.group(2)}"
                if full_info.strip():
                    notes.append(full_info.strip())

            # Password row
            elif row_label_lower == "password":
                password = full_info.strip() or "SuperPSX"

            # Voices / Voice row
            elif row_label_lower in ("voices", "voice"):
                voices = full_info.strip()

            # Screen Languages row
            elif row_label_lower in ("screen languages", "screenlanguages"):
                screen_languages = full_info.strip()

    # Default password
    if not password:
        password = "SuperPSX"

    # Default version from info table or section label
    if not version:
        # Try to extract from section label or table text
        full_text = soup.get_text(" ", strip=True)
        ver_match = VERSION_RE.search(full_text)
        if ver_match:
            version = normalize_version(full_text)

    full_text = soup.get_text(" ", strip=True)
    link_urls = [l.get("url") or "" for l in all_links]
    merged = normalize_formats(file_formats)
    for fmt in detect_formats([full_text], urls=link_urls):
        if fmt != "unknown" and fmt not in merged:
            merged.append(fmt)
    file_formats = merged or ["unknown"]

    return {
        "links": all_links,
        "title_id": title_id,
        "version": version,
        "region": region,
        "firmware": firmware,
        "firmware_compat": firmware_compat,
        "password": password,
        "voices": voices,
        "screen_languages": screen_languages,
        "file_formats": file_formats,
        "notes": notes,
        "sections": sections,
    }


# ---------------------------------------------------------------------------
# Phase 4: Combine game page + DLL page data into a package entry
# ---------------------------------------------------------------------------

def scrape_game(game_url: str) -> dict | None:
    """Scrape a single game: game page → DLL page → combined package.

    Returns a package dict matching the dlpsgame-ps5.json format, or None on failure.
    """
    # Step 1: Parse game page
    game_data = parse_game_page(game_url)
    if not game_data:
        log.warning("  ✗ No game data for %s", game_url)
        return None

    dll_url = game_data.get("dll_url")
    if not dll_url:
        log.warning("  ✗ No DLL page link found for %s", game_url)
        return None

    # Step 2: Parse DLL page
    dll_data = parse_dll_page(dll_url)
    if not dll_data:
        log.warning("  ✗ No DLL data for %s", dll_url)
        return None

    # Step 3: Combine data into package format
    info = game_data.get("info", {})
    title = game_data.get("title", "")

    # Title ID: prefer from DLL page (most reliable), then info table, then game URL
    title_id = dll_data.get("title_id")
    if not title_id:
        # Try from info table Version field
        version_str = info.get("Version", "")
        ppsa_match = PPSA_RE.search(version_str)
        if ppsa_match:
            title_id = ppsa_match.group(1).upper()
    if not title_id:
        # Try from game page HTML
        title_id = ""

    # Version: prefer from DLL page, then info table
    version = dll_data.get("version")
    if not version:
        update_str = info.get("Update", "")
        if update_str:
            version = normalize_version(update_str)
        else:
            version_str = info.get("Version", "")
            if version_str:
                version = normalize_version(version_str)
    if not version:
        version = "01.000"

    # Size: from info table (token propre) or DLL page notes (texte libre)
    size_str = info.get("Size", "")
    size_bytes = parse_size_bytes(size_str)
    if not size_bytes:
        # Try from DLL page notes — extraction sûre (anti-bug « to »)
        for note in dll_data.get("notes", []):
            sb, _ = extract_size(note)
            if sb:
                size_bytes = sb
                break

    # Region: from DLL page or info table
    region = dll_data.get("region")
    if not region:
        version_str = info.get("Version", "")
        region_match = re.search(r"[–\-]\s*([A-Z]{2,5})\b", version_str)
        if region_match:
            region = region_match.group(1)

    # Build description
    tags: list[str] = []
    if title_id:
        tags.append(title_id)
    # Version tag
    ver_tag_match = VERSION_RE.search(version) if version else None
    if ver_tag_match:
        tags.append(f"v{ver_tag_match.group(1)}")
    else:
        tags.append(f"v{version}")
    # Firmware compatibility
    firmware = dll_data.get("firmware")
    firmware_compat = dll_data.get("firmware_compat")
    if firmware:
        tags.append(firmware)
    if firmware_compat:
        tags.append(firmware_compat)
    if region:
        tags.append(region)

    desc_lines: list[str] = []
    if tags:
        desc_lines.append(f"Tags: {', '.join(tags)}")
    if size_str:
        desc_lines.append(f"Size: {size_str}")
    # Password
    password = dll_data.get("password", "SuperPSX")
    desc_lines.append(f"Password: {password}")
    # Firmware requirement
    if firmware:
        desc_lines.append(f"FW Required: {firmware}")
    if firmware_compat:
        desc_lines.append(f"FW Compat: {firmware_compat}")
    # Voices
    voices = dll_data.get("voices")
    if voices:
        desc_lines.append(f"Voices: {voices}")
    # Notes
    for note in dll_data.get("notes", []):
        if note and note not in (firmware, firmware_compat):
            desc_lines.append(note)

    description = "\n".join(desc_lines)

    # Download links (deduplicated by URL)
    seen_urls: set[str] = set()
    download_links: list[dict[str, str]] = []
    for link in dll_data.get("links", []):
        url = link.get("url", "")
        name = link.get("name", "Mirror")
        # Skip empty-URL text-only hosts (FileK etc.)
        if not url:
            log.debug("    skip text-only host: %s", name)
            continue

        if is_keepshield_url(url):
            group_prefix = ""
            if " - " in name:
                group_prefix = name.rsplit(" - ", 1)[0] + " - "
            resolved = resolve_keepshield(url)
            if resolved:
                for mirror in resolved:
                    m_url = mirror.get("url", "")
                    if not m_url or m_url in seen_urls:
                        continue
                    seen_urls.add(m_url)
                    m_name = f"{group_prefix}{mirror.get('mirror', 'Mirror')}"
                    download_links.append({"name": m_name, "url": m_url})
                continue

            log.debug("    Unresolved keepshield, raw link preserved: %s", url)

        # Skip non-host URLs
        if is_non_host_url(url):
            log.debug("    skip non-host: %s — %s", name, url)
            continue
        # Deduplicate
        if url in seen_urls:
            continue
        seen_urls.add(url)
        
        # CATEGORIZATION LOGIC
        group = detect_group(name) 
        clean_name = name.rsplit(" - ", 1)[-1] if " - " in name else name
        formatted_name = f"{group} - {clean_name}"
        
        download_links.append({"name": formatted_name, "url": url})

    if not download_links:
        log.warning("  ✗ No download links for %s", game_url)
        return None

    region = "EUR" if "EUR" in title.upper() else "USA"
    is_exfat = "exfat" in title.lower() or "exfat" in description.lower()
    suffix = " (exFAT)" if is_exfat else ""
    title_id = f"{title_id} – {region}{suffix}"

    formatted_links = []
    for link in download_links:
        group = detect_group(link["name"])
        clean_name = link["name"].rsplit(" - ", 1)[-1] if " - " in link["name"] else link["name"]
        formatted_links.append({"name": f"{group} - {clean_name}", "url": link["url"]})

    package: dict = {
        "titleId": title_id,
        "title": title,
        "version": version,
        "category": "game",
        "posterUrl": game_data.get("poster_url"),
        "description": description,
        "downloadLinks": formatted_links,
        "sizeBytes": size_bytes,
        "downloadSource": dll_url,
        "source": SITE_SOURCE,
        "fileFormat": dll_data.get("file_formats", ["unknown"]),
    }

    # Only include sizeBytes if known (null can cause issues in some consumers)
    if not size_bytes:
        del package["sizeBytes"]

    log.info("    ✓ %s — %d links", title, len(download_links))
    return package


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def scrape_all(
    max_pages: int | None,
    max_games: int | None,
    concurrency: int,
) -> tuple[list[dict], list[str]]:
    """Main scraping pipeline.

    1. Discover game URLs from category pages
    2. For each game, scrape game page + DLL page
    3. Return (packages, warnings)
    """
    game_urls = discover_game_urls(max_pages)
    if max_games is not None:
        game_urls = game_urls[:max_games]

    log.info("Scraping %d games (concurrency=%d)...", len(game_urls), concurrency)

    packages: list[dict] = []
    warnings: list[str] = []
    failed_urls: list[str] = []

    if concurrency <= 1:
        for i, url in enumerate(game_urls, 1):
            log.info("[%d/%d]", i, len(game_urls))
            try:
                pkg = scrape_game(url)
                if pkg:
                    packages.append(pkg)
                else:
                    warnings.append(url)
                    failed_urls.append(url)
            except Exception as exc:
                log.warning("  ✗ %s: %s", url, exc)
                warnings.append(f"{url}: {exc}")
                failed_urls.append(url)
            time.sleep(PAGE_DELAY)
    else:
        
        def _scrape_game_jittered(url: str) -> dict | None:
            time.sleep(random.uniform(0.1, 0.3))
            return scrape_game(url)

        with cf.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(_scrape_game_jittered, url): url for url in game_urls}
            for fut in cf.as_completed(futures):
                url = futures[fut]
                try:
                    pkg = fut.result()
                except Exception as exc:
                    warnings.append(f"{url}: {exc}")
                    failed_urls.append(url)
                    continue
                if pkg:
                    packages.append(pkg)
                else:
                    warnings.append(url)
                    failed_urls.append(url)

    # === Phase 2: Retry failed pages ===
    if failed_urls:
        log.info("=" * 60)
        log.info("Phase 2: retrying %d failed page(s)", len(failed_urls))
        log.info("=" * 60)

        # Invalidate cache for failed URLs
        for url in failed_urls:
            for cache_key_fn in [_cache_key]:
                cache_file = DISK_CACHE_DIR / cache_key_fn(url)
                if cache_file.exists():
                    try:
                        cache_file.unlink()
                    except Exception:
                        pass

        retry_success = 0
        still_failing: list[str] = []
        for i, url in enumerate(failed_urls, 1):
            log.info("[retry %d/%d]", i, len(failed_urls))
            try:
                pkg = scrape_game(url)
                if pkg:
                    packages.append(pkg)
                    retry_success += 1
                else:
                    still_failing.append(url)
            except Exception as exc:
                still_failing.append(url)
            time.sleep(PAGE_DELAY * 2)

        log.info(
            "Phase 2 complete: %d success, %d still failing",
            retry_success,
            len(still_failing),
        )
        warnings = still_failing

    # Sort by title
    packages.sort(key=lambda p: (p.get("title") or "").lower())
    return packages, warnings


def build_catalog(packages):
    # 1. The nested hierarchy you want for your program
    nested_catalog = {}
    for pkg in packages:
        title = pkg.get("title")
        variation = f"{pkg.get('titleid')}_{pkg.get('region')}"
        if pkg.get('is_exfat'):
            variation += "_exFAT"
        
        group = detect_group(pkg.get("label", ""))
        
        if title not in nested_catalog: nested_catalog[title] = {}
        if variation not in nested_catalog[title]: nested_catalog[title][variation] = {}
        if group not in nested_catalog[title][variation]: nested_catalog[title][variation][group] = []
        
        nested_catalog[title][variation][group].append({
            "host": pkg.get("mirror_name"),
            "url": pkg.get("url")
        })

    # 2. Return a structure that satisfies BOTH the merge script and your new hierarchy
    return {
        "name": "Scraped Catalog",
        "version": 1,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "packages": packages,       # THIS IS WHAT merge_catalogs.py NEEDS
        "nested_data": nested_catalog # THIS IS WHAT YOUR PROGRAM NEEDS
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SuperPSX PS5 Games Scraper — produces a JSON catalog "
                    "compatible with dlpsgame-ps5.json format.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Full scrape (all pages)
  python scrape_superpsx.py

  # Limited scrape for testing
  python scrape_superpsx.py --max-pages 2 --max-games 10

  # Custom output path
  python scrape_superpsx.py --out superpsx-ps5.json

  # Verbose mode with single-threaded scraping
  python scrape_superpsx.py --verbose --concurrency 1

  # With FlareSolverr backend (REQUIRED: superpsx.com is behind Cloudflare)
  python scrape_superpsx.py --http-backend flaresolverr --flaresolverr-url http://localhost:8191/v1
""",
    )
    parser.add_argument(
        "--max-pages", type=int, default=None,
        help="Limit the number of category pages to crawl",
    )
    parser.add_argument(
        "--max-games", type=int, default=None,
        help="Limit the number of games to scrape (useful for testing)",
    )
    parser.add_argument(
        "--out", type=Path, default=Path("superpsx-ps5.json"),
        help="Output JSON file path (default: superpsx-ps5.json)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=2,
        help="Number of threads for scraping game pages (default: 2). "
             "superpsx.com is plain curl without Cloudflare, so 3-4 is safe.",
    )
    parser.add_argument(
        "--delay", type=float, default=1.0,
        help="Delay in seconds between requests (default: 1.0)",
    )
    parser.add_argument(
        "--http-backend", choices=["curl", "flaresolverr"],
        default="curl",
        help="HTTP backend: 'curl' (local, unblocked IP) or 'flaresolverr' "
             "(Docker proxy, REQUIRED because superpsx.com is behind Cloudflare)".",
    )
    parser.add_argument(
        "--flaresolverr-url", default=None,
        help="FlareSolverr URL (default: http://localhost:8191/v1). For a pool "
             "For multi-instance setups, define FLARESOLVERR_URLS (CSV) as an environment variable instead..",
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Disable disk cache (.scrape_cache_superpsx/) — force full re-scrape",
    )
    parser.add_argument(
        "--mode", choices=["full", "incremental"], default="full",
        help="Scrape mode: 'full' (re-scrape everything) or 'incremental' "
             "(only new/updated games). Default: full",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable debug-level logging",
    )
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    # Apply configuration
    global _HTTP_BACKEND, FLARESOLVERR_URL, DISK_CACHE_ENABLED, PAGE_DELAY
    _HTTP_BACKEND = args.http_backend
    PAGE_DELAY = args.delay

    if args.no_cache:
        DISK_CACHE_ENABLED = False
    if args.flaresolverr_url:
        FLARESOLVERR_URL = args.flaresolverr_url

    # In FlareSolverr mode, the session (or multi-instance pool) is created first
    # to reuse the Chrome instance(s) (much faster).
    if _HTTP_BACKEND == "flaresolverr":
        init_flaresolverr_session()

    # FlareSolverr is single-session per instance: a single session does not support
    # parallelism. We force concurrency=1 UNLESS a multi-instance pool
    # is active: in that case, we allow min(requested concurrency, pool size).
    if _HTTP_BACKEND == "flaresolverr" and args.concurrency > 1:
        if _FS_POOL is not None and _FS_POOL.size > 1:
            new_conc = min(args.concurrency, _FS_POOL.size)
            if new_conc != args.concurrency:
                log.warning("FlareSolverr pool of %d instance(s) — concurrency reduced to %d",
                            _FS_POOL.size, new_conc)
            args.concurrency = new_conc
        else:
            log.warning("FlareSolverr (single session) does not support parallelism. "
                        "— concurrency forcé à 1")
            args.concurrency = 1

    log.info(
        "Starting SuperPSX scraper (backend: %s, concurrency: %d, delay: %.1fs, mode: %s)",
        _HTTP_BACKEND, args.concurrency, PAGE_DELAY, args.mode,
    )

    # Load manifest for incremental mode
    manifest = None
    if args.mode == "incremental":
        try:
            from scrape_manifest import ScrapeManifest
        except ImportError:
            ScrapeManifest = None  # type: ignore[assignment,misc]
        if ScrapeManifest:
            manifest = ScrapeManifest(path=".scrape_manifest_superpsx.json")
            log.info("Incremental mode: manifest has %d entries",
                     len(manifest._data.get("entries", {})))
        else:
            log.warning("scrape_manifest module not found — running in full mode")

    try:
        packages, warnings = scrape_all(
            max_pages=args.max_pages,
            max_games=args.max_games,
            concurrency=args.concurrency,
        )
    except KeyboardInterrupt:
        log.info("Interrupted by user — saving partial results")
        packages, warnings = [], []
    except Exception as exc:
        log.error("Fatal error: %s", exc)
        return 1
    finally:
        if _HTTP_BACKEND == "flaresolverr":
            destroy_flaresolverr_session()

    # Save manifest with scraped packages
    if manifest:
        for pkg in packages:
            source_url = pkg.get("downloadSource", "")
            if source_url:
                manifest.record(source_url, json.dumps(pkg, ensure_ascii=False), package=pkg)
        manifest.save()
        log.info("Manifest saved with %d entries", len(manifest._data.get("entries", {})))

    catalog = build_catalog(packages)

    # Write output
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    log.info(
        "Written: %s  (%d games, %d warnings)",
        args.out, len(packages), len(warnings),
    )

    # Write warnings file
    if warnings:
        warn_path = args.out.with_suffix(".warnings.txt")
        warn_path.write_text("\n".join(str(w) for w in warnings), encoding="utf-8")
        log.info("Warnings written: %s", warn_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
