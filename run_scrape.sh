#!/bin/bash
python3 scripts/import_exfat.py --out dlps-exfat.json
python3 scripts/scrape_superpsx.py --out superpsx.json

python3 scripts/merge_catalogs.py final_catalog.json dlps-exfat.json superpsx.json
