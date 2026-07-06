#!/usr/bin/env python3

from __future__ import annotations
import argparse
import json
import logging
import re
import sys
from pathlib import Path

LOG = logging.getLogger("merge_catalogs")

REAL_TITLEID_RE = re.compile(r"^[A-Z]{4}\d{3,}$")

def normalize_links(links):
    """Converts dictionary-based links to a flat list of objects."""
    if isinstance(links, list):
        return links
    if isinstance(links, dict):
        flat_list = []
        for category, link_list in links.items():
            for link in link_list:
                if 'name' in link and 'url' in link:
                    flat_list.append(link)
        return flat_list
    return []

def merge_key(pkg: dict) -> str:
    """Clé d'unicité d'un package."""
    tid = (pkg.get("titleId") or "").strip().upper()
    if REAL_TITLEID_RE.match(tid):
        return f"id:{tid}"
    title = (pkg.get("title") or "").strip().lower()
    title = re.sub(r"[^a-z0-9]+", " ", title).strip()
    return f"title:{title}"


def merge_links(out, new_links):
    if not isinstance(out, list):
        out = []

    link_map = {}
    
    for l in out:
        if isinstance(l, dict):
            url = l.get("url")
            if url:
                link_map[url.strip()] = l
        else:
            link_map[str(l).strip()] = {"name": "Link", "url": str(l).strip()}

    for link_data in new_links:
        if isinstance(link_data, dict):
            url = link_data.get("url")
            if url and url.strip() not in link_map:
                link_map[url.strip()] = link_data
        else:
            url = str(link_data)
            if url and url.strip() not in link_map:
                link_map[url.strip()] = {"name": "Link", "url": url.strip()}
            
    return list(link_map.values())

def richer(a, b):
    """Retourne la valeur 'la plus riche' entre a (existant) et b (entrant)."""
    if isinstance(a, str) or isinstance(b, str):
        a = a or ""
        b = b or ""
        return a if len(a) >= len(b) else b
    if isinstance(a, (int, float)) or isinstance(b, (int, float)):
        return a if (a or 0) >= (b or 0) else b
    return a if a else b


def union_list(a, b) -> list:
    """Union de deux valeurs liste-ou-scalaire, dédoublonnée, ordre préservé."""
    out: list = []
    for val in (a, b):
        if val is None:
            continue
        items = val if isinstance(val, list) else [val]
        for it in items:
            if it not in out:
                out.append(it)
    return out

ENRICHABLE_FIELDS = ("version", "description", "sizeBytes", "downloadSource", "category")
UNION_FIELDS = ("source", "fileFormat")
PRESERVE_FIELDS = (
    "_enrichedAt", "_rawgMatched", "metadata",
    "_igdbEnrichedAt", "_igdbMatched",
)

def _cover_rank(pkg: dict) -> int:
    """Qualité de la posterUrl d'un package (plus haut = meilleure jaquette).

    3 = jaquette IGDB (vraie cover portrait)
    2 = cover scrapée du site (og:image) ou autre source réelle
    1 = image RAWG (rawg.io : screenshot/hero paysage, PAS une jaquette)
    0 = pas de poster
    """
    url = (pkg.get("posterUrl") or "").lower()
    if not url:
        return 0
    if "images.igdb.com" in url or pkg.get("_igdbMatched") is True:
        return 3
    if "rawg.io" in url:
        return 1
    return 2


def merge_package(base: dict, extra: dict) -> dict:
    """Fusionne deux packages représentant le même jeu."""
    merged = dict(base)
    merged["downloadLinks"] = merge_links(
        base.get("downloadLinks", []), extra.get("downloadLinks", [])
    )
    for field in ENRICHABLE_FIELDS:
        if field in base or field in extra:
            merged[field] = richer(base.get(field), extra.get(field))
    for field in UNION_FIELDS:
        if field in base or field in extra:
            merged[field] = union_list(base.get(field), extra.get(field))

    rank_base, rank_extra = _cover_rank(base), _cover_rank(extra)
    if rank_extra > rank_base:
        merged["posterUrl"] = extra.get("posterUrl")
    elif rank_base > rank_extra:
        merged["posterUrl"] = base.get("posterUrl")
    elif "posterUrl" in base or "posterUrl" in extra:
        merged["posterUrl"] = base.get("posterUrl") or extra.get("posterUrl")

    for field in PRESERVE_FIELDS:
        val = base.get(field)
        if val is None:
            val = extra.get(field)
        if val is not None:
            merged[field] = val

    for cand in (base.get("titleId"), extra.get("titleId")):
        if cand and REAL_TITLEID_RE.match(cand.strip().upper()):
            merged["titleId"] = cand
            break

    if not merged.get("sizeBytes"):
        merged.pop("sizeBytes", None)
    return merged


def load_catalog(path: Path, *, fresh: bool = False) -> dict:
    """Charge un catalogue et valide sa structure.

    Garde-fou (item 6) : une source FRAÎCHE doit impérativement exposer une clé
    'packages' qui est une LISTE. Un scrape partiel ou corrompu (objet, null,
    clé absente) est rejeté avec un message clair plutôt que de produire une
    fusion silencieusement dégradée.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "packages" not in data:
        kind = "source fraîche" if fresh else "catalogue"
        raise ValueError(
            f"{path} : {kind} invalide — clé 'packages' absente "
            "(pas un catalogue Pegasus DL valide)."
        )
    if not isinstance(data["packages"], list):
        kind = "source fraîche" if fresh else "catalogue"
        got = type(data["packages"]).__name__
        raise ValueError(
            f"{path} : {kind} invalide — 'packages' doit être une LISTE "
            f"(reçu : {got}). Scrape partiel ou fichier corrompu, rejeté."
        )
    return data

RC_OK = 0
RC_USAGE = 2
RC_REFUSED = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fusionne plusieurs catalogues Pegasus DL en un seul.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("output", help="Fichier de sortie (catalogue fusionné).")
    parser.add_argument(
        "inputs", nargs="+",
        help="Catalogues d'entrée : sources fraîches d'abord, catalogue existant "
             "ensuite (le dernier si plusieurs entrées, sauf --existing).",
    )
    parser.add_argument(
        "--existing", default=None,
        help="Chemin explicite du catalogue EXISTANT (référence du seuil). "
             "Par défaut : la dernière entrée quand il y en a plusieurs.",
    )
    parser.add_argument(
        "--min-fresh-ratio", type=float, default=0.5,
        help="Seuil anti-run-dégradé : refuse la fusion si le total des packages "
             "frais < ratio × taille du catalogue existant (défaut : 0.5).",
    )
    parser.add_argument(
        "--allow-shrink", action="store_true",
        help="Bypass du seuil : autorise un total frais inférieur (catalogue qui "
             "rétrécit légitimement). N'AVERTIT plus, fusionne quand même.",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="Mode full explicite : désactive entièrement le seuil anti-dégradé "
             "(scrape complet attendu, pas d'incrémental).",
    )
    return parser


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )

    if len(argv) < 3:
        build_parser().print_help()
        return RC_USAGE
    args = build_parser().parse_args(argv[1:])

    out_path = Path(args.output)
    in_paths = [Path(p) for p in args.inputs]

    existing_path: Path | None = None
    if args.existing:
        existing_path = Path(args.existing)
        fresh_paths = [p for p in in_paths if p != existing_path]
        if existing_path not in in_paths:
            fresh_paths = list(in_paths)
    elif len(in_paths) > 1:
        existing_path = in_paths[-1]
        fresh_paths = in_paths[:-1]
    else:
        fresh_paths = list(in_paths)

    fresh_catalogs: list[tuple[Path, dict]] = []
    fresh_total = 0
    try:
        for path in fresh_paths:
            cat = load_catalog(path, fresh=True)
            fresh_catalogs.append((path, cat))
            fresh_total += len(cat["packages"])

        existing_cat: dict | None = None
        if existing_path is not None:
            existing_cat = load_catalog(existing_path, fresh=False)
    except (ValueError, json.JSONDecodeError) as exc:
        LOG.error("MERGE REFUSÉ : %s", exc)
        return RC_USAGE

    if existing_cat is not None and existing_path is not None:
        existing_total = len(existing_cat["packages"])
        threshold = args.min_fresh_ratio * existing_total

        if args.full:
            LOG.info(
                "Mode --full : seuil anti-dégradé désactivé "
                "(frais=%d, existant=%d).", fresh_total, existing_total,
            )
        elif existing_total > 0 and fresh_total < threshold:
            if args.allow_shrink:
                LOG.warning(
                    "Run dégradé toléré (--allow-shrink) : %d packages frais "
                    "< %.0f%% × %d existants. Fusion poursuivie.",
                    fresh_total, args.min_fresh_ratio * 100, existing_total,
                )
            else:
                LOG.error(
                    "MERGE REFUSÉ : %d packages frais < %.0f%% × %d existants "
                    "(seuil=%.0f). Scrape probablement partiel (timeout Cloudflare ?). "
                    "Catalogue existant CONSERVÉ intact. "
                    "Utilisez --allow-shrink ou --full pour forcer.",
                    fresh_total, args.min_fresh_ratio * 100, existing_total,
                    threshold,
                )
                return RC_REFUSED

    merge_inputs = list(fresh_catalogs)
    if existing_cat is not None and existing_path is not None:
        merge_inputs.append((existing_path, existing_cat))

    by_key: dict[str, dict] = {}
    order: list[str] = []
    stats = []
    catalog_name = "Catalogue fusionné"
    
    for path, cat in merge_inputs:
        if not catalog_name or catalog_name == "Catalogue fusionné":
            catalog_name = cat.get("name", catalog_name)
            
        added = updated = 0
        
        for pkg in cat["packages"]:
            if isinstance(pkg.get("downloadLinks"), dict):
                pkg["downloadLinks"] = [link for sublist in pkg["downloadLinks"].values() for link in sublist]
            
            key = merge_key(pkg)
            if key in by_key:
                existing_pkg = by_key[key]
                
                by_key[key] = merge_package(existing_pkg, pkg)
                updated += 1
            else:
                by_key[key] = pkg
                order.append(key)
                added += 1
        
        stats.append((path.name, len(cat["packages"]), added, updated))

    packages = [by_key[k] for k in order]
    if "api" in str(out_path):
        result = {
            "packages": packages,
            "metadata": {"count": len(packages), "source": "merged"}
        }
    else:
        result = {
            "name": catalog_name,
            "version": 1,
            "packages": packages,
        }
    try:
        content = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"DEBUG: Successfully wrote {len(result.get('packages', []))} packages to {out_path}")
    except Exception as e:
        print(f"CRITICAL ERROR: Failed to write JSON file! Error: {e}")
        raise

    print("Fusion terminée :")
    for name, total, added, updated in stats:
        print(f"  - {name:32s} {total:5d} jeux  (+{added} nouveaux, {updated} fusionnés)")
    print(f"  => Final file updated: dlpsgame-ps5.json")
    return RC_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv))
