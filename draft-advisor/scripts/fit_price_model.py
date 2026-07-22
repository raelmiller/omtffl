#!/usr/bin/env python3
"""Fit the OMTFFL price model: what the league pays for a player, given
the points he scored the season before the auction, by position.

Emits data/price_model.json with:
  - per-position price bands (p25/median/p75 by prior-points band)
  - season liquidity context (money per team, spend totals)
  - per-manager bidding priors for rival prediction (club affinity,
    marquee appetite, position spend shares)

Deliberately simple + explainable: banded quantiles, not regression.
"""
import json
from collections import defaultdict
from pathlib import Path
from statistics import median, quantiles

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# prior-points bands: label -> (lo, hi)
BANDS = [
    ("0-49", 0, 49), ("50-79", 50, 79), ("80-99", 80, 99),
    ("100-124", 100, 124), ("125-149", 125, 149), ("150-179", 150, 179),
    ("180+", 180, 10_000),
]

PREV = {"2023-24": "2022/23", "2024-25": "2023/24", "2025-26": "2024/25"}


def band_of(pts):
    for label, lo, hi in BANDS:
        if lo <= pts <= hi:
            return label
    return None


def main():
    fpl = json.loads((DATA / "fpl_data.json").read_text())
    sales = json.loads((DATA / "enriched_sales.json").read_text())
    hist = {int(k): v for k, v in fpl["history_past"].items()}

    def prior_points(sale):
        if not sale["fpl_id"]:
            return None
        want = PREV[sale["season"]]
        for h in hist.get(sale["fpl_id"], []):
            if h["season_name"] == want:
                return h["total_points"]
        return None  # new to the league that year

    pairs = defaultdict(list)  # (position, band) -> [price]
    n_used = 0
    for s in sales:
        if s["cost"] is None:
            continue
        pp = prior_points(s)
        if pp is None:
            continue
        b = band_of(pp)
        pairs[(s["position"], b)].append(s["cost"])
        n_used += 1

    bands_out = defaultdict(dict)
    for (pos, b), prices in sorted(pairs.items()):
        prices.sort()
        n = len(prices)
        if n >= 4:
            q = quantiles(prices, n=4)
            p25, p50, p75 = q[0], median(prices), q[2]
        else:
            p25 = min(prices)
            p50 = median(prices)
            p75 = max(prices)
        bands_out[pos][b] = {
            "n": n,
            "p25": round(p25, 2),
            "median": round(p50, 2),
            "p75": round(p75, 2),
            "max": max(prices),
        }

    # per-manager priors for "who's in on this player"
    mgr = defaultdict(lambda: {
        "seasons": set(), "clubs": defaultdict(int),
        "pos_spend": defaultdict(float), "spend": 0.0,
        "bids": [], "big_bids": 0,
    })
    for s in sales:
        m = mgr[s["manager"]]
        m["seasons"].add(s["season"])
        if s["club"]:
            m["clubs"][s["club"]] += 1
        if s["cost"] is not None:
            m["pos_spend"][s["position"]] += s["cost"]
            m["spend"] += s["cost"]
            m["bids"].append(s["cost"])
            if s["cost"] >= 10:
                m["big_bids"] += 1

    managers = {}
    for name, m in mgr.items():
        total = m["spend"] or 1
        yrs = len(m["seasons"])
        managers[name] = {
            "seasons": yrs,
            "club_affinity": {
                c: n for c, n in sorted(m["clubs"].items(), key=lambda kv: -kv[1])
                if n >= 3
            },
            "pos_share": {p: round(v / total, 3) for p, v in m["pos_spend"].items()},
            "max_bid": max(m["bids"]) if m["bids"] else 0,
            "big_bids_per_season": round(m["big_bids"] / yrs, 2),
            "avg_spend_per_season": round(m["spend"] / yrs, 2),
        }

    seasons = {}
    for season in PREV:
        srows = [s for s in sales if s["season"] == season and s["cost"] is not None]
        teams = {s["manager"] for s in sales if s["season"] == season}
        seasons[season] = {
            "teams": len(teams),
            "spent": round(sum(s["cost"] for s in srows), 2),
            "pool": len(teams) * 50,
        }

    out = {
        "trained_on_sales": n_used,
        "prior_points_bands": [b[0] for b in BANDS],
        "price_bands": bands_out,
        "seasons": seasons,
        "managers": managers,
    }
    (DATA / "price_model.json").write_text(json.dumps(out, indent=1))

    print(f"trained on {n_used} priced sales with known prior-season points\n")
    for pos in ["GK", "DEF", "MID", "FWD"]:
        print(pos)
        for label, *_ in BANDS:
            r = bands_out.get(pos, {}).get(label)
            if r:
                print(f"  {label:8s} n={r['n']:3d}  £{r['p25']:.2f} / £{r['median']:.2f} / £{r['p75']:.2f}  (max £{r['max']})")


if __name__ == "__main__":
    main()
