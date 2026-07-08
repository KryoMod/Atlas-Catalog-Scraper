import json
import os

def merge_catalogs():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(script_dir)
    
    exfat_path = os.path.join(root_dir, 'dlps-exfat.json')
    superpsx_path = os.path.join(root_dir, 'superpsx.json')
    output_path = os.path.join(root_dir, 'PS5_catalog.json')

    catalog = {}
    
    for path, source in [(exfat_path, 'exFAT'), (superpsx_path, 'superPSX')]:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            for pkg in data.get("packages", []):
                print(f"DEBUG: Processing package from {source}: {pkg.get('titleId')}")
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
