#!/usr/bin/env python3
"""Join draft history to FPL identities and build the per-player breakdown.

For every player ever sold in an OMTFFL draft: their sale price + buyer in
each season, matched to an FPL id where possible so we can attach the
points they actually scored that season (from history_past) and current
factsheet stats.

Matching strategy (names in the sheets are informal):
  1. normalise: lowercase, strip accents/punctuation
  2. candidate FPL players by surname (last token) or web_name containment
  3. disambiguate by club (with alias map), then first initial, then minutes
  4. manual override map for the stubborn cases

Outputs draft-advisor/data/player_breakdown.json
"""
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

CLUB_ALIASES = {
    "BHA": "BHA", "BRI": "BHA",          # Brighton
    "BRE": "BRE", "BRF": "BRE",          # Brentford
    "NFO": "NFO", "FOR": "NFO",          # Forest (also a position typo trap)
    "SHU": "SHU", "SHE": "SHU",
    "WOL": "WOL",
    "SPURS": "TOT",
}

# token-level spelling fixes: how the sheets spell it -> how FPL spells it
MANUAL_TOKENS = {
    "soudawara": "sugawara",
    "guesson": "guessand",     # Evann Guessand
    "hermansson": "hermansen", # Mads Hermansen
    "outtara": "ouattara",     # Dango Ouattara
    "jorgenson": "jorgensen",
    "zabaryni": "zabarnyi",
    "aaronsen": "aaronson",
    "tels": "tel",             # Mathys Tel
    "totti": "toti",           # Toti Gomes
    "fleming": "flemming",     # Zian Flemming
    "zrikzee": "zirkzee",
    "norgaard": "norgaard",
    "christain": "christian",
}

# characters NFKD won't decompose to ASCII
CHAR_FIXES = str.maketrans({"ø": "o", "Ø": "O", "ß": "ss", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L"})


def norm(s):
    s = (s or "").translate(CHAR_FIXES)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z ]", "", s.lower().replace("-", " ")).strip()
    return " ".join(MANUAL_TOKENS.get(t, t) for t in s.split())


def main():
    fpl = json.loads((DATA / "fpl_data.json").read_text())
    draft = json.loads((DATA / "draft_history.json").read_text())

    players = fpl["players"]
    by_surname = defaultdict(list)
    for p in players:
        p["_norm_web"] = norm(p["web_name"])
        p["_norm_second"] = norm(p["second_name"])
        p["_norm_first"] = norm(p["first_name"])
        for tok in set(
            p["_norm_web"].split() + p["_norm_second"].split()
        ):
            if tok:
                by_surname[tok].append(p)

    def match(sale):
        raw = norm(sale["player"])
        if not raw:
            return None
        toks = raw.split()
        club = CLUB_ALIASES.get(sale["club"], sale["club"])

        # gather candidates: any token hits a surname index, or web_name contains
        cands = []
        for tok in toks:
            cands.extend(by_surname.get(tok, []))
        if not cands:
            for p in players:
                if raw and (raw in p["_norm_web"] or raw in (p["_norm_first"] + " " + p["_norm_second"])):
                    cands.append(p)
        if not cands:
            return None
        # dedupe
        seen, uniq = set(), []
        for p in cands:
            if p["id"] not in seen:
                seen.add(p["id"])
                uniq.append(p)
        cands = uniq
        if len(cands) == 1:
            return cands[0]

        # prefer club match (note: player may have moved; club is from sale season)
        club_matches = [p for p in cands if p["team_short"] == club]
        if len(club_matches) == 1:
            return club_matches[0]
        pool = club_matches or cands

        # first-initial disambiguation ("E. Martinez", "L. Martinez")
        m = re.match(r"^([a-z])\b", raw)
        if m and len(pool) > 1:
            ini = m.group(1)
            ini_matches = [p for p in pool if p["_norm_first"].startswith(ini)]
            if len(ini_matches) == 1:
                return ini_matches[0]
            pool = ini_matches or pool

        # full first-name containment
        fn_matches = [
            p for p in pool
            if p["_norm_first"] and p["_norm_first"].split()[0] in toks
        ]
        if len(fn_matches) == 1:
            return fn_matches[0]
        pool = fn_matches or pool

        # last resort: most minutes (most prominent player wins)
        return max(pool, key=lambda p: p.get("minutes") or 0)

    hist_by_id = {int(k): v for k, v in fpl["history_past"].items()}

    def season_points(fpl_id, season):
        # draft season "2023-24" -> FPL season_name "2023/24"
        want = season.replace("-", "/")
        for h in hist_by_id.get(fpl_id, []):
            if h["season_name"] == want:
                return h["total_points"]
        return None

    # group sales by identity (fpl id when matched, else normalised name)
    groups = {}
    unmatched = []
    for sale in draft["sales"]:
        p = match(sale)
        key = f"fpl_{p['id']}" if p else f"name_{norm(sale['player'])}"
        g = groups.setdefault(
            key,
            {
                "key": key,
                "fpl_id": p["id"] if p else None,
                "display": p["web_name"] if p else sale["player"],
                "position": sale["position"],
                "current_club": p["team_short"] if p else None,
                "status": p["status"] if p else None,
                "last_season_points": p["total_points"] if p else None,
                "xgi": p.get("expected_goal_involvements") if p else None,
                "penalties": (p.get("penalties_order") == 1) if p else False,
                "corners": (p.get("corners_and_indirect_freekicks_order") or 9) <= 2 if p else False,
                "freekicks": (p.get("direct_freekicks_order") or 9) <= 2 if p else False,
                "sales": {},
            },
        )
        pts = season_points(p["id"], sale["season"]) if p else None
        price = sale["cost"]
        # Ali 23-24 prices were never recorded
        price_known = not (sale["season"] == "2023-24" and sale["manager"] == "Ali")
        g["sales"][sale["season"]] = {
            "price": price if price_known else None,
            "buyer": sale["manager"],
            "club_then": sale["club"],
            "points_that_season": pts,
        }
        if not p:
            unmatched.append((sale["season"], sale["player"], sale["club"]))

    rows = sorted(
        groups.values(),
        key=lambda g: -max(
            (s["price"] or 0) for s in g["sales"].values()
        ),
    )

    out = {
        "generated_from": {
            "fpl_baked_at": fpl["baked_at"],
            "sales": len(draft["sales"]),
            "unique_players": len(rows),
            "matched_to_fpl": sum(1 for r in rows if r["fpl_id"]),
        },
        "players": rows,
    }
    (DATA / "player_breakdown.json").write_text(json.dumps(out, indent=1))

    # flat per-sale rows enriched with FPL identity + points that season,
    # for downstream consumers (manager profiles, pricing model)
    enriched = []
    for sale in draft["sales"]:
        p = match(sale)
        price_known = not (sale["season"] == "2023-24" and sale["manager"] == "Ali")
        enriched.append(
            {
                **sale,
                "cost": sale["cost"] if price_known else None,
                "fpl_id": p["id"] if p else None,
                "fpl_name": p["web_name"] if p else None,
                "points_that_season": season_points(p["id"], sale["season"]) if p else None,
            }
        )
    (DATA / "enriched_sales.json").write_text(json.dumps(enriched, indent=1))

    print(json.dumps(out["generated_from"], indent=2))
    if unmatched:
        print(f"\nUnmatched sales ({len(unmatched)}):")
        for u in sorted(set(unmatched)):
            print("  ", u)


if __name__ == "__main__":
    main()
