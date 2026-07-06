#!/usr/bin/env python3
"""
Centralized analysis of file sizes (sizeBytes).
=========================================================
Fixes a confirmed bug: the old regex
    r"(\\d+(?:[.,]\\d+)?)\\s*([KMGT])[\\s]?(?:B|O|b|o)\\b"
accepted a single-letter unit [KMGT] followed by B **or** O/o, meaning the
English word "to" (= T+o) was interpreted as Terabytes. Result: entries
with "Size: 2023 to" → 2.2 petabytes (5 erroneous entries in production).

Strategy (verified against 759 actual packages: 0 legitimate French size formats):
  - Extraction from free text: accepts ONLY complete English units
    (KB/MB/GB/TB). "to" does not contain "TB" → never captured.
  - Priority anchoring on "SIZE:" to avoid mistaking a firmware version
    ("5.50 to 7.xx") or a year for a file size.
  - parse_size_bytes() (token already isolated, e.g., "Size" table cell)
    also accepts a valid French token (where the entire string == a size),
    without ever matching "to" in the middle of a sentence.
"""
from __future__ import annotations

import re

_POW = {"K": 1, "M": 2, "G": 3, "T": 4}
_NUM = r"(\d{1,4}(?:[.,]\d{1,2})?)"

_SIZE_ANCHORED = re.compile(r"(?i)\bSIZE\s*[:\-–]\s*" + _NUM + r"\s?([KMGT])B\b")
_SIZE_FREE = re.compile(r"(?i)" + _NUM + r"\s?([KMGT])B\b")

_TOKEN_EN = re.compile(r"(?i)" + _NUM + r"\s?([KMGT])B\b")
_TOKEN_FR = re.compile(r"(?i)^\s*" + _NUM + r"\s?([KMGT])[O]\s*$")


def _to_bytes(value_str: str, scale: str) -> int:
    return int(float(value_str.replace(",", ".")) * (1024 ** _POW[scale.upper()]))


def parse_size_bytes(value) -> int | None:
    """Parse a size that is ALREADY isolated. (token : '54GB', '12 Go', '1.5 GB', int…)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 else None
    s = str(value).replace("\xa0", " ")
    m = _TOKEN_EN.search(s) or _TOKEN_FR.match(s)
    return _to_bytes(m.group(1), m.group(2)) if m else None


def extract_size(text) -> tuple[int | None, str | None]:
    """Extracts (bytes, label) from free-form text. Anchored to "SIZE:" first."""
    if not text:
        return None, None
    t = str(text).replace("\xa0", " ")
    m = _SIZE_ANCHORED.search(t) or _SIZE_FREE.search(t)
    if not m:
        return None, None
    return _to_bytes(m.group(1), m.group(2)), f"{m.group(1)} {m.group(2).upper()}B"


def extract_size_bytes(text) -> int | None:
    return extract_size(text)[0]


# ---------------------------------------------------------------------------
# Auto-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    GB, MB, TB = 1024**3, 1024**2, 1024**4
    ok = True

    def check(label, got, expected):
        global ok
        status = "OK " if got == expected else "FAIL"
        if got != expected:
            ok = False
        print(f"  [{status}] {label}: {got}  (attendu {expected})")

    check("'Size: 54GB'", extract_size_bytes("blah Size: 54GB blah"), 54 * GB)
    check("'117GB'", extract_size_bytes("Game 117GB total"), 117 * GB)
    check("'1.5 GB'", extract_size_bytes("1.5 GB"), int(1.5 * GB))
    check("'850 MB'", extract_size_bytes("850 MB"), 850 * MB)
    check("'15.9gb'", extract_size_bytes("size 15.9gb"), int(15.9 * GB))
    check("'SIZE : 54 GB'", extract_size_bytes("SIZE : 54 GB"), 54 * GB)
    check("'2023 to' (bug)", extract_size_bytes("BIOHAZARD RE 4 Size:2023 to"), None)
    check("'6.50 to' (bug)", extract_size_bytes("6.50 to"), None)
    check("'5 to' (bug)", extract_size_bytes("Devil May Cry 5 to"), None)
    check("'2023 to 2024'", extract_size_bytes("released 2023 to 2024"), None)
    check("'5.50 to 7.xx'", extract_size_bytes("FW 5.50 to 7.xx"), None)
    check("'SIZE: 2 to 4 players'", extract_size_bytes("SIZE: 2 to 4 players"), None)
    check("token '117 GB'", parse_size_bytes("117 GB"), 117 * GB)
    check("token '12 Go' (FR)", parse_size_bytes("12 Go"), 12 * GB)
    check("token '2 to 4 players'", parse_size_bytes("2 to 4 players"), None)
    check("token int", parse_size_bytes(57982058496), 57982058496)
    check("token None", parse_size_bytes(None), None)

    print("ALL OK" if ok else "SOME FAILURES")
    raise SystemExit(0 if ok else 1)
