import json

def merge_catalogs():
    catalog = {}
    for path, source in [('dlps-exfat.json', 'exFAT'), ('superpsx.json', 'superPSX')]:
        with open(path, 'r') as f:
            data = json.load(f)
            for pkg in data.get("packages", []):
                raw_tid = pkg["titleId"].replace('\u2013', '-').replace('–', '-').split('-')[0].strip()
                if raw_tid not in catalog:
                    catalog[raw_tid] = {
                        "baseTitleId": raw_tid,
                        "title": pkg["title"],
                        "variations": []
                    }
                catalog[raw_tid]["variations"].append({
                    "name": pkg["titleId"],
                    "source": source,
                    "links": pkg.get("downloadLinks", [])
                })
    with open('PS5_catalog.json', 'w', encoding='utf-8') as f:
        json.dump({"packages": list(catalog.values())}, f, indent=2, ensure_ascii=False)
    print("DONE: Saved to PS5_catalog.json")

merge_catalogs()
