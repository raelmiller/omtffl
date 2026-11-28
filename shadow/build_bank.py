#!/usr/bin/env python3
"""Build the question bank for About Right — a daily quiz about this league.

Five families, in the order they matter:

  AUCTION   this season's draft, scoped to one manager: what they bought
  SQUAD     last season's ownership, scoped to one manager: who they owned,
            started and benched, week by week
  RECORDS   league superlatives — most goals, most yellows, most wins
  MARKET    the draft as a whole — price brackets, positions, clubs
  FOOTBALL  what happened on the pitch, kept deliberately thin

A round is 2 AUCTION + 1 SQUAD + 1 RECORDS + 1 other, so four of five
questions are about somebody in the league rather than about football.

TWO KINDS OF QUESTION. Most are "open": name ANY valid answer, scored on
speed times rarity, so an obscure right answer beats an obvious one. RECORDS
are "top": one right answer, which cannot use a rarity scale, so they score on
speed alone and the answer is a manager rather than a player.

Usage:
    python3 shadow/build_bank.py [--out PATH]

Writes one JSON file the quiz page fetches: every player and manager who can
be an answer, each with a rarity weight, and every question with its complete
set of valid answer ids. Answer sets are filters over the league's own data,
so they are exact by construction and nothing is maintained by hand.

Manager-answer questions ("name a manager who beat RM") were tried and
dropped: eleven to fifteen of the sixteen managers beat any given rival last
season, so almost any name is right and there is no longshot to find.

NAMING. This season's team names are in squads.json, so auction questions use
them. The 2025/26 archive deliberately stores initials only, so squad
questions use those; the page carries a legend mapping the two.

DIFFICULTY. `ease` counts how many of the season's best-known players are
valid answers — a proxy for "would a name you'd actually think of be right".
It governs the FOOTBALL family, where football knowledge is all you have. It
is deliberately NOT a gate on manager-scoped questions: what makes "name a
player Isakly drafted" answerable is knowing the league, which this measure
cannot see and would wrongly mark as hard.

RARITY has two sources. Last season: the player's prominence. The draft: the
price paid — naming the £26.50m man is obvious, naming a £0 pick is the
longshot the game is named for.
"""
import argparse
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
ARCH = DATA / "season2025-26"
DRAFT = DATA / "squads.json"
OUT = DATA / "bank.json"          # --out overrides
POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
XI = 11
PROMINENT = 120
MIN_MGR = 5             # a manager-scoped question needs this many answers
                        # (a draft squad is 2/5/5/3, so five is a whole
                        #  positional group — enough when scoped to one team)
MIN_OPEN = 10           # an open question needs more, there being no scope
EASE_MIN = 6            # FOOTBALL only: below this it is a lottery
EASE_EASY = 12
SQUAD_WEEKS = (5, 14, 23, 32)


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def rank_weights(items, key):
    """1.00x for the most obvious, 3.00x for the most obscure. Ties share.

    `key` is a prominence measure — season points, or price paid — so the list
    sorts ASCENDING and position 0, the least prominent, takes the 3.00x.
    Sorting it the other way silently pays most for naming Haaland, which is
    the opposite of the game.
    """
    order = sorted(items, key=key)
    n = len(order)
    out, i = {}, 0
    while i < n:
        j = i
        while j + 1 < n and key(order[j + 1]) == key(order[i]):
            j += 1
        pct = ((i + j) / 2) / (n - 1) if n > 1 else 1.0
        for k in range(i, j + 1):
            out[order[k]] = round(1.0 + 2.0 * (1 - pct), 2)
        i = j + 1
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT,
                    help=f"where to write the bank (default {OUT})")
    args = ap.parse_args()
    out = args.out

    for p in (ARCH, DRAFT):
        if not p.exists():
            raise SystemExit(f"missing {p} — this needs the shadow data")

    # ---------------- last season ----------------
    meta = json.loads((ARCH / "players.json").read_text())
    names = {int(k): v for k, v in meta["names"].items()}
    positions = {int(k): POS.get(v, "?") for k, v in meta["positions"].items()}
    clubs = {int(k): v for k, v in meta["clubs"].items()}
    pclub = {int(k): v for k, v in meta["player_clubs"].items()}

    weeks = {}
    for f in sorted(ARCH.glob("gw*.json")):
        gw = json.loads(f.read_text())
        weeks[gw["gameweek"]] = {e["id"]: e["stats"] for e in gw["elements"]}
    lineups = {int(g): v for g, v in
               json.loads((ARCH / "lineups.json").read_text())["lineups"].items()}
    managers = sorted(lineups[1].keys())

    tot = defaultdict(lambda: defaultdict(int))
    for stats in weeks.values():
        for pid, s in stats.items():
            for k, v in s.items():
                if isinstance(v, (int, float)):
                    tot[pid][k] += v
    played = [p for p, t in tot.items() if t["minutes"] > 0]
    fameL = rank_weights(played, lambda p: tot[p]["total_points"])
    famous = {norm(names.get(p, "")) for p in
              sorted(played, key=lambda p: -tot[p]["total_points"])[:PROMINENT]}

    # ---------------- the draft ----------------
    draft = json.loads(DRAFT.read_text())
    teamname = {t["key"]: t["team"] for t in draft["teams"]}
    # squads.json holds the team names as they stood on auction night. People
    # rename mid-season, and a quiz played now should use the name they answer
    # to now, so the live league's standings override where they differ.
    # Checked against FPL Draft league 65448 at gameweek 11: one rename.
    teamname.update({"SG": "Shaun of the Dedic"})
    picks = []
    for t in draft["teams"]:
        for s in t["squad"]:
            picks.append((f"D{s['id']}", s["name"], s["position"], s["club"],
                          float(s["price"]), t["key"]))
    dprice = {p[0]: p[4] for p in picks}
    dmgr = {p[0]: p[5] for p in picks}
    dclub = {p[0]: p[3] for p in picks}
    dpos = {p[0]: p[2] for p in picks}
    dname = {p[0]: p[1] for p in picks}
    fameD = rank_weights(list(dprice), lambda d: dprice[d])
    dkeys = sorted(teamname)

    def namekey(x):
        return norm(names.get(int(x[1:]), "")) if x[0] == "L" else norm(dname[x])

    bank = []

    def add(fam, q, answers, scoped):
        a = sorted({x for x in answers})
        if len(a) < (MIN_MGR if scoped else MIN_OPEN):
            return
        ease = len([x for x in a if namekey(x) in famous])
        # The ease floor is about football knowledge, so it gates only the
        # football family. A manager-scoped question is answerable from
        # knowing the league, which this measure cannot see.
        if fam == "FOOTBALL" and ease < EASE_MIN:
            return
        bank.append({"fam": fam, "kind": "open", "q": q, "a": a, "e": ease,
                     "t": "easy" if ease >= EASE_EASY else "med"})

    # ============ AUCTION (manager-scoped, this season) ============
    for k in dkeys:
        T = teamname[k]
        mine = [d for d in dprice if dmgr[d] == k]
        add("AUCTION", f"Name a player {T} drafted this season", mine, True)
        add("AUCTION", f"Name a player {T} paid £3m or more for",
            [d for d in mine if dprice[d] >= 3], True)
        add("AUCTION", f"Name a player {T} picked up for under £1m",
            [d for d in mine if dprice[d] < 1], True)
        add("AUCTION", f"Name a player {T} paid under £3m for",
            [d for d in mine if dprice[d] < 3], True)
        add("AUCTION", f"Name an outfield player {T} drafted",
            [d for d in mine if dpos[d] != "GK"], True)
        add("AUCTION", f"Name a defender or midfielder {T} drafted",
            [d for d in mine if dpos[d] in ("DEF", "MID")], True)
        for pos, word in (("DEF", "defender"), ("MID", "midfielder")):
            add("AUCTION", f"Name a {word} {T} drafted",
                [d for d in mine if dpos[d] == pos], True)

    # ============ SQUAD (manager-scoped, last season) ============
    def squad(m, g):
        return [e["id"] for e in lineups[g][m]]

    for m in managers:
        ever, allyear = set(), set(squad(m, 1))
        for g in weeks:
            ever |= set(squad(m, g))
            allyear &= set(squad(m, g))
        add("SQUAD", f"Name a player {m} owned at any point last season",
            [f"L{p}" for p in ever], True)
        add("SQUAD", f"Name a player {m} owned in every single gameweek",
            [f"L{p}" for p in allyear], True)
        for g in SQUAD_WEEKS:
            add("SQUAD", f"Name a player {m} started in Gameweek {g}",
                [f"L{e['id']}" for e in lineups[g][m] if e["pos"] <= XI], True)
            add("SQUAD", f"Name a player {m} left on the bench in Gameweek {g}",
                [f"L{e['id']}" for e in lineups[g][m] if e["pos"] > XI], True)

    # ============ MARKET (the draft as a whole) ============
    add("MARKET", "Name a player who went for £10m or more at the auction",
        [d for d in dprice if dprice[d] >= 10], False)
    add("MARKET", "Name a player who went for £5m or more at the auction",
        [d for d in dprice if dprice[d] >= 5], False)
    add("MARKET", "Name a player who was drafted for nothing — £0",
        [d for d in dprice if dprice[d] == 0], False)
    add("MARKET", "Name a player who went for under £1m at the auction",
        [d for d in dprice if dprice[d] < 1], False)
    for pos, word in (("GK", "goalkeeper"), ("DEF", "defender"),
                      ("MID", "midfielder"), ("FWD", "forward")):
        add("MARKET", f"Name a {word} who was drafted this season",
            [d for d in dprice if dpos[d] == pos], False)
    for club in sorted(set(dclub.values())):
        add("MARKET", f"Name a {club} player who was drafted this season",
            [d for d in dprice if dclub[d] == club], False)
    for g in SQUAD_WEEKS:
        owned = {p for m in managers for p in squad(m, g)}
        add("MARKET", f"Name a player NOBODY in the league owned in Gameweek {g}",
            [f"L{p}" for p, s in weeks[g].items()
             if s["minutes"] > 0 and p not in owned], False)

    # ============ FOOTBALL (kept thin) ============
    add("FOOTBALL", "Name a player who was sent off last season",
        [f"L{p}" for p, t in tot.items() if t["red_cards"] > 0], False)
    add("FOOTBALL", "Name a player who scored an own goal last season",
        [f"L{p}" for p, t in tot.items() if t["own_goals"] > 0], False)
    add("FOOTBALL", "Name a goalkeeper who saved a penalty last season",
        [f"L{p}" for p, t in tot.items() if t["penalties_saved"] > 0], False)
    add("FOOTBALL", "Name a player who missed a penalty last season",
        [f"L{p}" for p, t in tot.items() if t["penalties_missed"] > 0], False)
    add("FOOTBALL", "Name a player who got 10 or more assists last season",
        [f"L{p}" for p, t in tot.items() if t["assists"] >= 10], False)
    add("FOOTBALL", "Name a player who scored 10 or more goals last season",
        [f"L{p}" for p, t in tot.items() if t["goals_scored"] >= 10], False)
    add("FOOTBALL", "Name a defender who scored 5 or more goals last season",
        [f"L{p}" for p, t in tot.items()
         if t["goals_scored"] >= 5 and positions.get(p) == "DEF"], False)
    add("FOOTBALL", "Name a player who returned 15+ points in a single gameweek",
        [f"L{p}" for p in tot if any(weeks[g].get(p, {}).get("total_points", 0) >= 15
                                     for g in weeks)], False)
    add("FOOTBALL", "Name a player who played in all 38 gameweeks",
        [f"L{p}" for p in tot if sum(1 for g in weeks
                                     if weeks[g].get(p, {}).get("minutes", 0) > 0) == 38],
        False)

    # ============ RECORDS (superlatives: the answer is a manager) ============
    # A superlative has one right answer, so it cannot use the rarity scale —
    # these are a separate KIND, scored on speed alone. Aggregates count the
    # XI a manager actually STARTED each week, not everyone they owned: those
    # are the goals and cards that really were theirs that weekend.
    fx = json.loads((ARCH / "fixtures.json").read_text())["fixtures"]
    agg = defaultdict(lambda: defaultdict(int))
    for g, wk in weeks.items():
        for m in managers:
            for e in lineups[g][m]:
                if e["pos"] <= XI:
                    st = wk.get(e["id"], {})
                    for f in ("goals_scored", "assists", "yellow_cards",
                              "red_cards", "saves", "bonus", "own_goals"):
                        agg[m][f] += st.get(f, 0)
                    if st.get("clean_sheets", 0) and positions.get(e["id"]) in ("GK", "DEF"):
                        agg[m]["cs"] += 1
    wins, pf, bestgw = defaultdict(int), defaultdict(int), defaultdict(int)
    for x in fx:
        h, a = x["home"], x["away"]
        hs, as_ = x["actual"]["home"], x["actual"]["away"]
        pf[h] += hs; pf[a] += as_
        bestgw[h] = max(bestgw[h], hs); bestgw[a] = max(bestgw[a], as_)
        if hs > as_: wins[h] += 1
        elif as_ > hs: wins[a] += 1

    def addtop(q, table, unit, most=True):
        """Whoever tops `table` is the answer — ties share it."""
        best = (max if most else min)(table.values())
        winners = sorted(m for m in table if table[m] == best)
        if len(winners) > 2:
            return          # a five-way tie is not a superlative
        bank.append({"fam": "RECORDS", "kind": "top", "q": q,
                     "a": [f"M{w}" for w in winners], "e": 0, "t": "med",
                     "note": ", ".join(winners) + f" — {best} {unit}"})

    addtop("Whose starting XI scored the most goals last season?",
           {m: agg[m]["goals_scored"] for m in managers}, "goals")
    addtop("Whose starting XI got the most assists last season?",
           {m: agg[m]["assists"] for m in managers}, "assists")
    addtop("Whose starting XI collected the most yellow cards last season?",
           {m: agg[m]["yellow_cards"] for m in managers}, "yellows")
    addtop("Whose starting XI collected the FEWEST yellow cards last season?",
           {m: agg[m]["yellow_cards"] for m in managers}, "yellows", most=False)
    addtop("Whose starting XI picked up the most red cards last season?",
           {m: agg[m]["red_cards"] for m in managers}, "reds")
    addtop("Whose starting XI kept the most clean sheets last season?",
           {m: agg[m]["cs"] for m in managers}, "clean sheets")
    addtop("Whose goalkeepers made the most saves last season?",
           {m: agg[m]["saves"] for m in managers}, "saves")
    addtop("Whose starting XI earned the most bonus points last season?",
           {m: agg[m]["bonus"] for m in managers}, "bonus points")
    addtop("Which manager won the most matches last season?",
           dict(wins), "wins")
    addtop("Which manager won the FEWEST matches last season?",
           dict(wins), "wins", most=False)
    addtop("Which manager scored the most points across last season?",
           dict(pf), "points")
    addtop("Which manager posted the highest single-gameweek score last season?",
           dict(bestgw), "points")

    # ---------------- emit ----------------
    used = {x for q in bank for x in q["a"]}
    players = {}
    for x in used:
        if x.startswith("M"):
            continue            # managers are added below, with their aliases
        if x.startswith("L"):
            p = int(x[1:])
            players[x] = {"n": names.get(p, "?"), "k": norm(names.get(p, "")),
                          "p": positions.get(p, "?"),
                          "c": clubs.get(pclub.get(p, 0), {}).get("short", "???"),
                          "f": fameL.get(p, 2.0)}
        else:
            players[x] = {"n": dname[x], "k": norm(dname[x]), "p": dpos[x],
                          "c": dclub[x], "f": fameD[x], "pr": dprice[x]}
    # Last season's initials map onto this season's team keys for the twelve
    # who are still here; GA and RJ have left, so they answer to initials only.
    KEYMAP = {"CC": "CCP", "LR": "LRC"}
    for m in managers:
        team = teamname.get(KEYMAP.get(m, m))
        ks = [norm(m)] + ([norm(team)] if team else [])
        players[f"M{m}"] = {"n": m, "k": norm(m), "ks": ks,
                            "t": team or "", "f": 1.0, "mgr": True}

    for i, q in enumerate(bank):
        q["id"] = f"q{i:04d}"

    legend = [{"k": k, "t": teamname[k]} for k in dkeys]
    out.write_text(json.dumps(
        {"season": "2025/26 + 2026/27 draft", "legend": legend,
         "players": players, "questions": bank},
        separators=(",", ":"), ensure_ascii=False))

    print(f"{len(bank)} questions, {len(players)} players\n")
    share = {"AUCTION": "2 of 5", "SQUAD": "1 of 5", "RECORDS": "1 of 5",
             "MARKET": "1 of 5 (shared)", "FOOTBALL": "1 of 5 (shared)"}
    print(f"{'FAMILY':<10}{'N':>5}   share of a round")
    for fam in ("AUCTION", "SQUAD", "RECORDS", "MARKET", "FOOTBALL"):
        n = len([q for q in bank if q["fam"] == fam])
        print(f"{fam:<10}{n:>5}   {share[fam]}")
    mgr = len([q for q in bank if q["fam"] in ("AUCTION", "SQUAD", "RECORDS")])
    print(f"\n{mgr} of {len(bank)} questions ({100*mgr//len(bank)}%) name a "
          f"specific manager.\nFour of every five asked are about one of you.")
    print("\nRecords:")
    for q in [x for x in bank if x["fam"] == "RECORDS"]:
        print(f"  {q['q']}\n      -> {q['note']}")
    print("\nSample:")
    for fam in ("AUCTION", "SQUAD"):
        for q in [x for x in bank if x["fam"] == fam][:3]:
            print(f"  [{fam:<7}] {q['q']}  ({len(q['a'])} valid)")
    print(f"\n{out}  {out.stat().st_size/1024:.1f} KB")


if __name__ == "__main__":
    main()
