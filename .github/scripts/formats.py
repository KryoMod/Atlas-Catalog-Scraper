#!/usr/bin/env python3
from __future__ import annotations
import re
from urllib.parse import unquote, urlparse

INSTALL_FORMATS: tuple[str, ...] = (
    "FPKG", "FFPKG", "FFPFSC", "exFAT", "Folder", "PKG", "APR-EMU",
)
CONTAINER_FORMATS: tuple[str, ...] = ("RAR", "ZIP", "7z")

_TEXT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bff\s*pfsc\b", re.I), "FFPFSC"),
    (re.compile(r"\bffpfs\b", re.I), "FFPFSC"),
    (re.compile(r"\bff\s*pkg\b", re.I), "FFPKG"),
    (re.compile(r"\bf\s*pkg\b", re.I), "FPKG"),
    (re.compile(r"\bfake\s*pkg\b", re.I), "FPKG"),
    (re.compile(r"\bex\s*fat\b", re.I), "exFAT"),
    (re.compile(r"\b(?:dossier|folder|extracted|extrait)\b", re.I), "Folder"),
    (re.compile(r"apr[\s\-]*emu", re.I), "APR-EMU"),
    (re.compile(r"ampr[\s\-]*emu", re.I), "APR-EMU"),
    (re.compile(r"\bpkg\b", re.I), "PKG"),
]

_EXT_MAP: list[tuple[str, str]] = [
    (".ffpfsc", "FFPFSC"),
    (".ffpkg", "FFPKG"),
    (".fpkg", "FPKG"),
    (".pkg", "PKG"),
    (".exfat", "exFAT"),
    (".rar", "RAR"),
    (".7z", "7z"),
    (".zip", "ZIP"),
]

_BRACKET_TAG_RE = re.compile(r"\[([A-Za-z0-9\-]+)\]")
_BACKPORT_RE = re.compile(r"backport\s*(\d+\.(?:\d+|xx))", re.I)
_BACKPORT_BARE_RE = re.compile(r"\bbackport\b", re.I)

_CANON = {
    "fpkg": "FPKG", "ffpkg": "FFPKG", "ffpfsc": "FFPFSC", "ffpfs": "FFPFSC",
    "exfat": "exFAT", "folder": "Folder", "dossier": "Folder",
    "pkg": "PKG", "apr-emu": "APR-EMU", "apremu": "APR-EMU",
    "ampr-emu": "APR-EMU", "rar": "RAR", "zip": "ZIP", "7z": "7z",
}

def canon_tag(tag: str) -> str | None:
    if not tag:
        return None
    key = re.sub(r"[\s_]+", "", tag.strip().lower())
    key = key.replace("emu", "-emu") if key in ("apremu", "ampremu") else key
    return _CANON.get(key) or _CANON.get(tag.strip().lower())

def _iter_texts(texts) -> list[str]:
    if texts is None:
        return []
    if isinstance(texts, str):
        return [texts]
    return [str(t) for t in texts if t]


def classify(
    texts=None,
    *,
    urls=None,
    filenames=None,
) -> dict[str, list[str]]:

    blob = " \n ".join(_iter_texts(texts))
    name_blob = " ".join(_iter_texts(filenames))
    url_names: list[str] = []
    for u in _iter_texts(urls):
        try:
            url_names.append(unquote(urlparse(u).path or u))
        except Exception:
            url_names.append(u)
    combined = "\n".join([blob, name_blob, " ".join(url_names)])

    install: list[str] = []
    container: list[str] = []
    backport: list[str] = []

    def add(bucket: list[str], label: str) -> None:
        if label and label not in bucket:
            bucket.append(label)

    for m in _BRACKET_TAG_RE.finditer(combined):
        c = canon_tag(m.group(1))
        if c in INSTALL_FORMATS:
            add(install, c)
        elif c in CONTAINER_FORMATS:
            add(container, c)

    for pattern, label in _TEXT_PATTERNS:
        if pattern.search(combined):
            add(install, label)

    for url in _iter_texts(urls) + _iter_texts(filenames):
        low = url.lower()
        try:
            path = urlparse(low).path or low
        except Exception:
            path = low
        for ext, label in _EXT_MAP:
            if ext in path:
                if label in INSTALL_FORMATS:
                    add(install, label)
                else:
                    add(container, label)

    seen_fw: set[str] = set()
    for m in _BACKPORT_RE.finditer(combined):
        fw = m.group(1).lower()
        if fw not in seen_fw:
            seen_fw.add(fw)
            add(backport, f"Backport {fw}")
    if not backport and _BACKPORT_BARE_RE.search(combined):
        add(backport, "Backport")

    install.sort(key=lambda x: INSTALL_FORMATS.index(x) if x in INSTALL_FORMATS else 99)
    return {"install": install, "container": container, "backport": backport}

def detect_formats(
    texts=None,
    *,
    urls=None,
    filenames=None,
) -> list[str]:
    c = classify(texts, urls=urls, filenames=filenames)
    flat = [*c["install"], *c["container"], *c["backport"]]
    out: list[str] = []
    for f in flat:
        if f not in out:
            out.append(f)
    return out or ["unknown"]

def normalize_formats(values) -> list[str]:
    if values is None:
        return []
    items = values if isinstance(values, (list, tuple)) else [values]
    out: list[str] = []
    for v in items:
        v = str(v).strip()
        if not v:
            continue
        if v.lower().startswith("backport"):
            label = v if " " in v else "Backport"
        else:
            label = canon_tag(v) or v
        if label not in out:
            out.append(label)
    return out

def primary_install_format(formats) -> str | None:
    """Returns the primary (most specific) installation format or None.."""
    norm = normalize_formats(formats)
    for f in INSTALL_FORMATS:
        if f in norm:
            return f
    return None

def display_label(formats) -> str:
    """Short, readable label for the UI / Pegasus

    Exemples :
      ["FPKG", "RAR"]                 -> "FPKG"
      ["exFAT", "FFPFSC", "RAR"]      -> "exFAT · FFPFSC"
      ["Folder"]                      -> "Folder"
      ["PKG", "Backport 4.xx"]        -> "PKG · Backport 4.xx"
      []                              -> ""
    """
    norm = normalize_formats(formats)
    install = [f for f in INSTALL_FORMATS if f in norm]
    backport = [f for f in norm if f.lower().startswith("backport")]
    parts = install[:2] + backport[:1]
    return " · ".join(parts)

if __name__ == "__main__":
    cases = [
        ("Game (v01.000) [APR-EMU]", None, ["APR-EMU"]),
        ("SIZE: 54GB exFAT release, FFPFSC container", None, ["FFPFSC", "exFAT"]),
        ("Backport 4.xx fix included, FPKG", None, ["FPKG", "Backport 4.xx"]),
        ("normal folder / dossier extrait", None, ["Folder"]),
        ("", ["https://x/[DLPSGAME.COM]-PPSA1-Game (v01.000) [FPKG].rar"], ["FPKG", "RAR"]),
        ("just pkg", None, ["PKG"]),
        ("nothing here", None, ["unknown"]),
    ]
    ok = True
    for text, urls, expected in cases:
        got = detect_formats(text, urls=urls)
        status = "OK " if got == expected else "FAIL"
        if got != expected:
            ok = False
        print(f"  [{status}] detect_formats({text!r}, urls={urls}) -> {got}  (attendu {expected})")
    print("display_label exFAT/FFPFSC/RAR ->", display_label(["exFAT", "FFPFSC", "RAR"]))
    print("primary exFAT/FFPFSC ->", primary_install_format(["exFAT", "FFPFSC"]))
    print("ALL OK" if ok else "SOME FAILURES")
    raise SystemExit(0 if ok else 1)
