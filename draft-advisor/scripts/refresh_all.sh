#!/usr/bin/env bash
# Refresh the whole dataset + rebuild the standalone advisor from a bootstrap
# file. Usage:  bash scripts/refresh_all.sh [path/to/bootstrap.json]
set -e
cd "$(dirname "$0")/.."
BS="${1:-data/raw/bootstrap_latest.json}"
python3 scripts/update_from_bootstrap.py "$BS"
python3 scripts/build_player_breakdown.py
python3 scripts/build_manager_dossiers.py
python3 scripts/fit_price_model.py
python3 scripts/build_standalone.py
echo "Done. Fresh tool: draft-advisor/advisor-standalone.html"
