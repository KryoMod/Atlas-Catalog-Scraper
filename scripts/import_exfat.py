#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from formats import detect_formats, normalize_formats

DEFAULT_URL = "https://pippo26442999.github.io/.exFAT/exFAT.json"
DEFAULT_OUT = Path("exfat-ps5.fresh.json")
REQUEST_TIMEOUT = 25
REQUEST_RETRIES = 4

LOG = logging.getLogger("import_exfat")

MIRROR_PATTERNS = [
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

FAKE_TITLE_RE = re.compile(
    r"\b(fake|honeypot|honey\s*pot|do\s*not\s*download|virus|malware|scam|trap)\b",
    re.IGNORECASE,
)

SUSPICIOUS_MARKERS = {
    "honeypot",
    "honey pot",
    "fake",
    "do not download",
    "malware",
    "virus",
    "trap",
}

FAKE_HOST_MARKERS = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "example.com",
    "example.org",
    "fake",
    "honeypot",
}


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="[%(levelname)s] %(message)s",
        stream=sys.stderr,
    )


def b64url_decode(data: str) -> bytes:
    payload = data.strip().replace("-", "+").replace("_", "/")
    payload += "=" * ((4 - len(payload) % 4) % 4)
    return base64.b64decode(payload)


def normalize_group(value: str | None) -> str:
    t = (value or "").strip().lower()
    if "backport" in t:
        return "Backport"
    if "dlc" in t:
        return "DLC"
    if "dump" in t:
        return "Dump"
    if "exfat" in t:
        return "exFAT"
    return "Standard"


def detect_mirror(url: str, fallback: str | None = None) -> str:
    u = (url or "").lower()
    for pattern, name in MIRROR_PATTERNS:
        if pattern in u:
            return name
    if fallback:
        t = fallback.strip()
        if t:
            return t
    return "Mirror"


def parse_size_bytes(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 else None
    s = str(value).strip().lower().replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(tb|gb|mb|kb|b)", s)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2)
    power = {"b": 0, "kb": 1, "mb": 2, "gb": 3, "tb": 4}[unit]
    return int(num * (1024 ** power))


def fetch_json(url: str, timeout: int, retries: int) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = Request(url, headers={"User-Agent": "dlpsgame-exfat-importer/1.0"})
            with urlopen(req, timeout=timeout) as response:
                data = response.read().decode("utf-8")
            return json.loads(data)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            wait_s = min(2 ** attempt, 20)
            LOG.warning("Fetch exFAT échouée (%d/%d): %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(wait_s)
    raise RuntimeError(f"""Unable to retrieve exFAT JSON after {retries} attempts: {last_error}""")


def _extract_url_from_obj(obj: Any) -> str | None:
    if isinstance(obj, str):
        return obj.strip() or None
    if isinstance(obj, dict):
        for key in ("url", "href", "link", "download", "src"):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def decrypt_linklock(url: str) -> str:
    parsed = urlparse(url)
    fragment = parsed.fragment
    if not fragment:
        return url

    candidate = unquote(fragment)
    payload_obj: dict[str, Any] | None = None

    for part in (candidate, parse_qs(candidate).get("data", [""])[0]):
        if not part:
            continue
        try:
            payload_obj = json.loads(part)
            break
        except Exception:
            try:
                payload_obj = json.loads(b64url_decode(part).decode("utf-8"))
                break
            except Exception:
                continue

    if not isinstance(payload_obj, dict) or "e" not in payload_obj:
        return url

    try:
        encrypted = b64url_decode(str(payload_obj["e"]))
        salt = b64url_decode(str(payload_obj.get("s"))) if payload_obj.get("s") else b"\x00" * 16
        iv = b64url_decode(str(payload_obj.get("i"))) if payload_obj.get("i") else b"\x00" * 12
        key = hashlib.pbkdf2_hmac("sha256", b"pippo", salt, 100000, dklen=32)

        tag = None
        if payload_obj.get("t"):
            tag = b64url_decode(str(payload_obj["t"]))
            ciphertext = encrypted
        else:
            ciphertext = encrypted[:-16]
            tag = encrypted[-16:]

        decryptor = Cipher(algorithms.AES(key), modes.GCM(iv, tag)).decryptor()
        plain = decryptor.update(ciphertext) + decryptor.finalize()
        text = plain.decode("utf-8", errors="replace").strip()

        if text.startswith("{"):
            nested = json.loads(text)
            nested_url = _extract_url_from_obj(nested)
            if nested_url:
                return nested_url
        if text.startswith("http"):
            return text
        return url
    except Exception as exc:
        LOG.debug("Unable to retrieve exFAT JSON after {retries} attempts: (%s): %s", url, exc)
        return url


def iter_records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("packages", "games", "data", "items", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def iter_links(raw_links: Any) -> list[tuple[str, str | None, str]]:
    out: list[tuple[str, str | None, str]] = []
    if isinstance(raw_links, list):
        for item in raw_links:
            if isinstance(item, str):
                out.append(("", None, item))
            elif isinstance(item, dict):
                out.append((str(item.get("name") or ""), item.get("group") and str(item.get("group")), str(item.get("url") or item.get("href") or item.get("link") or "")))
    elif isinstance(raw_links, dict):
        for group, values in raw_links.items():
            if isinstance(values, str):
                out.append(("", str(group), values))
            elif isinstance(values, list):
                for item in values:
                    if isinstance(item, str):
                        out.append(("", str(group), item))
                    elif isinstance(item, dict):
                        out.append((str(item.get("name") or ""), str(item.get("group") or group), str(item.get("url") or item.get("href") or item.get("link") or "")))
                    else:
                        item_url = _extract_url_from_obj(item)
                        if item_url:
                            out.append(("", str(group), item_url))
    return out


def is_fake_entry(record: dict[str, Any], links: list[dict[str, str]]) -> bool:
    title = str(record.get("title") or record.get("name") or "")
    description = str(record.get("description") or record.get("desc") or "")
    tags = record.get("tags")
    if isinstance(tags, list):
        tags_text = " ".join(str(t) for t in tags)
    else:
        tags_text = str(tags or "")

    combined = f"{title} {description} {tags_text}".lower()
    if FAKE_TITLE_RE.search(combined):
        return True

    if record.get("fake") is True or record.get("honeypot") is True:
        return True

    if any(marker in combined for marker in SUSPICIOUS_MARKERS):
        return True

    for link in links:
        host = (urlparse(link["url"]).hostname or "").lower()
        if any(marker in host for marker in FAKE_HOST_MARKERS):
            return True

    return False


def build_package(record: dict[str, Any]) -> dict[str, Any] | None:
    title = str(record.get("title") or record.get("name") or "").strip()
    if not title:
        return None

    title_id = str(
        record.get("titleId")
        or record.get("titleID")
        or record.get("title_id")
        or record.get("id")
        or ""
    ).strip()
    if not title_id or not re.match(r"^[A-Z]{4}\d{3,}$", title_id.upper()):
        tags = record.get("tags")
        if isinstance(tags, list):
            for tag in tags:
                m = re.search(r"\b([A-Z]{4}\d{5,})\b", str(tag))
                if m:
                    title_id = m.group(1)
                    break
    if not title_id:
        digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:10].upper()
        title_id = f"GAME_{digest}"
    tags = record.get("tags")
    if isinstance(tags, list):
        tags_str = " ".join(str(t) for t in tags).upper()
        region = ""
        if "USA" in tags_str: region = "USA"
        elif "EUR" in tags_str: region = "EUR"
        elif "JPN" in tags_str: region = "JPN"
        
        if region:
            title_id = f"{title_id} – {region} (exFAT)"
        else:
            title_id = f"{title_id} (exFAT)"

    version = str(record.get("version") or "1.0").strip() or "1.0"
    if version == "1.0":
        tags = record.get("tags")
        if isinstance(tags, list):
            for tag in tags:
                m = re.match(r"v(\d+\.\d+)", str(tag), re.IGNORECASE)
                if m:
                    version = m.group(1)
                    break
    poster_url = str(record.get("posterUrl") or record.get("poster") or record.get("image") or "").strip()
    description = str(record.get("description") or record.get("desc") or "").strip()
    size_bytes = parse_size_bytes(record.get("sizeBytes") or record.get("size"))

    source_url = str(record.get("downloadSource") or record.get("source") or record.get("url") or DEFAULT_URL).strip()

    extracted_links: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    raw_links = record.get("downloadLinks")
    if raw_links is None:
        raw_links = record.get("links")

    if raw_links is None:
        FLAT_SKIP = {"image", "title", "size", "how_to_play", "tags", "apr_emu"}
        flat_links: list[dict[str, str]] = []
        for k, v in record.items():
            if k in FLAT_SKIP or k.startswith("credits_"):
                continue
            if not isinstance(v, str) or not v.strip():
                continue
            if not v.startswith(("http://", "https://")):
                continue
            if "standard" in k:
                group = "Standard"
            elif "backport7xx" in k:
                group = "Backport"
            elif "backport4xx" in k:
                group = "Backport"
            elif "backport" in k:
                group = "Backport"
            elif "dlc" in k:
                group = "DLC"
            elif "dump" in k:
                group = "Dump"
            else:
                group = ""
            flat_links.append({"url": v, "_key": k, "_group": group})
        raw_links = flat_links

    for item in raw_links:
        if isinstance(item, dict) and "_key" in item:
            raw_url = item.get("url", "")
            raw_group = item.get("_group", "")
            raw_name = item.get("_key", "")
        else:
            if isinstance(item, dict):
                raw_name = str(item.get("name") or "")
                raw_group = item.get("group") and str(item.get("group"))
                raw_url = str(item.get("url") or item.get("href") or item.get("link") or "")
            elif isinstance(item, (list, tuple)) and len(item) >= 3:
                raw_name, raw_group, raw_url = item[0], item[1], item[2]
            else:
                continue
        url = (raw_url or "").strip()
        if not url:
            continue
        if "#" in url:
            url = decrypt_linklock(url)
        if not url.startswith(("http://", "https://")):
            continue
        if url in seen_urls:
            continue

        group = normalize_group(raw_group or raw_name)
        mirror = detect_mirror(url, fallback=(raw_name or None))
        if group in ("Backport", "DLC", "Dump"):
            name = f"{group} - {mirror}"
        else:
            name = f"exFAT - {mirror}"

        seen_urls.add(url)
        extracted_links.append({"name": name, "url": url})

    if not extracted_links:
        return None
    if is_fake_entry(record, extracted_links):
        return None

    rec_fmt = record.get("fileFormat")
    if rec_fmt:
        file_format = normalize_formats(rec_fmt) or ["exFAT"]
    else:
        file_format = detect_formats(
            [title, description, str(record.get("tags") or "")],
            urls=[l["url"] for l in extracted_links],
        )
        if file_format == ["unknown"]:
            file_format = ["exFAT"]

    return {
        "titleId": title_id,
        "title": title,
        "version": version,
        "category": str(record.get("category") or "game"),
        "posterUrl": poster_url,
        "description": description,
        "downloadLinks": extracted_links,
        "sizeBytes": size_bytes,
        "downloadSource": source_url,
        "source": "exFAT",
        "fileFormat": file_format,
    }


def build_catalog(packages: list[dict[str, Any]], source_url: str) -> dict[str, Any]:
    return {
        "name": "exFAT PS5",
        "version": 1,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": source_url,
        "packages": packages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Import exFAT JSON into Pegasus-compatible catalog")
    parser.add_argument("--url", default=DEFAULT_URL, help="exFAT JSON URL")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output catalog path")
    parser.add_argument("--timeout", type=int, default=REQUEST_TIMEOUT, help="HTTP timeout (seconds)")
    parser.add_argument("--retries", type=int, default=REQUEST_RETRIES, help="HTTP retries")
    parser.add_argument("--verbose", action="store_true", help="Verbose logs")
    args = parser.parse_args()

    setup_logging(args.verbose)

    try:
        payload = fetch_json(args.url, timeout=args.timeout, retries=args.retries)
    except Exception as exc:
        LOG.error("Unable to import exFAT: %s", exc)
        return 1

    records = iter_records(payload)
    packages: list[dict[str, Any]] = []
    filtered = 0
    for record in records:
        package = build_package(record)
        if package is None:
            filtered += 1
            continue
        packages.append(package)

    packages.sort(key=lambda p: (p.get("title") or "").lower())
    catalog = build_catalog(packages, args.url)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")

    LOG.info(
        "exFAT import complete: raw=%d filtered=%d packages=%d out=%s",
        len(records),
        filtered,
        len(packages),
        args.out,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
