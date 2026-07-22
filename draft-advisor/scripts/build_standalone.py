#!/usr/bin/env python3
"""Build a single double-clickable advisor file with the data inlined.

Produces advisor-standalone.html: advisor.html with the four data files
embedded as window.__BAKED__, so it opens straight from the filesystem
(no local web server) while still connecting to the live auction socket.

Re-run this whenever the data re-bakes (e.g. the day before the draft).
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

def load(name):
    return json.loads((DATA / name).read_text())

baked = {
    "fpl": load("fpl_data.json"),
    "model": load("price_model.json"),
    "dossiers": load("manager_dossiers.json"),
    "sales": load("enriched_sales.json"),
}
# </script> inside the JSON would close the tag early; escape it
payload = json.dumps(baked, separators=(",", ":")).replace("</", "<\\/")
inject = f'<script>window.__BAKED__={payload};</script>\n'

html = (ROOT / "advisor.html").read_text()
# insert the data just before the main app script
marker = '<script>\n"use strict";'
if marker not in html:
    raise SystemExit("could not find the main <script> to inject before")
out = html.replace(marker, inject + marker, 1)

target = ROOT / "advisor-standalone.html"
target.write_text(out)
mb = target.stat().st_size / 1e6
print(f"wrote {target.name} ({mb:.1f} MB) — {len(baked['fpl']['players'])} players, "
      f"baked {baked['fpl']['baked_at'][:10]}")
print("double-click it to open; no server needed.")
