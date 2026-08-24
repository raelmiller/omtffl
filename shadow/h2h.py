#!/usr/bin/env python3
"""Head-to-head league table for the shadow season.

Scores each gameweek, resolves it against the fixture list, and builds the
standings. Where the league has recorded its own result for a gameweek, ours
is shown alongside it — a second, independent check on the engine at team
level rather than per player.

Usage
-----
    python3 shadow/h2h.py              # table + per-gameweek results
    python3 shadow/h2h.py --compare    # our scores vs the league's actuals
    python3 shadow/h2h.py --markdown   # write STANDINGS.md for the repo
"""
import json
import sys
from pathlib import Path

from score_league import best_xi, load_positions
from scoring import score_entry

DATA = Path(__file__).resolve().parent / "data"

WIN, DRAW = 3, 1


def gameweek_scores(path, squads, positions):
    gw = json.loads(path.read_text())
    pts = {}
    for el in gw["elements"]:
        pos = positions.get(el["id"])
        if pos is not None:
            pts[el["id"]] = score_entry(el, pos)
    scores = {}
    for team in squads["teams"]:
        total, _, _ = best_xi(team["squad"], pts)
        scores[team["key"]] = total
    return gw, scores


def standings_before(gameweek, squads=None, fixtures=None, positions=None):
    """The H2H table as it stood going into `gameweek`, best team first.

    Used by the waiver run, which orders claims from the bottom of the table
    upwards — so it has to be the table at the moment claims are processed,
    not the final one.
    """
    squads = squads or json.loads((DATA / "squads.json").read_text())
    if fixtures is None:
        fixtures = json.loads((DATA / "fixtures.json").read_text())
    positions = positions or load_positions()

    table = {t["key"]: dict(Pts=0, PF=0, PA=0) for t in squads["teams"]}
    by_gw = {}
    for fx in fixtures["fixtures"]:
        by_gw.setdefault(fx["gameweek"], []).append(fx)

    for f in sorted(DATA.glob("gw*.json")):
        n = int(f.stem[2:])
        if n >= gameweek:
            continue
        _, scores = gameweek_scores(f, squads, positions)
        for fx in by_gw.get(n, []):
            h, a = fx["home"], fx["away"]
            hs, as_ = scores.get(h, 0), scores.get(a, 0)
            for t, sf, sa in ((h, hs, as_), (a, as_, hs)):
                table[t]["PF"] += sf
                table[t]["PA"] += sa
                table[t]["Pts"] += WIN if sf > sa else DRAW if sf == sa else 0

    return [k for k, _ in sorted(
        table.items(), key=lambda kv: (-kv[1]["Pts"], -(kv[1]["PF"] - kv[1]["PA"]), -kv[1]["PF"]))]


def main():
    compare = "--compare" in sys.argv
    markdown = "--markdown" in sys.argv
    md = []

    for name in ("squads.json", "fixtures.json"):
        if not (DATA / name).exists():
            sys.exit(f"No data/{name} — run import_squads.py / build_fixtures.py first.")
    squads = json.loads((DATA / "squads.json").read_text())
    fixtures = json.loads((DATA / "fixtures.json").read_text())
    positions = load_positions()

    managers = sorted(t["key"] for t in squads["teams"])
    names = {t["key"]: t.get("team", t["key"]) for t in squads["teams"]}
    table = {m: dict(P=0, W=0, D=0, L=0, PF=0, PA=0, Pts=0) for m in managers}

    by_gw = {}
    for fx in fixtures["fixtures"]:
        by_gw.setdefault(fx["gameweek"], []).append(fx)

    files = sorted(DATA.glob("gw*.json"))
    if not files:
        sys.exit("No gameweek files — run the fetch workflow first.")

    deltas = []
    for f in files:
        gw, scores = gameweek_scores(f, squads, positions)
        n = gw["gameweek"]
        state = ("final" if gw.get("data_checked")
                 else "PROVISIONAL" if gw.get("finished")
                 else "IN PROGRESS — round not complete")
        print(f"\n{'='*66}\nGameweek {n} ({state})\n{'='*66}")
        md.append(f"\n### Gameweek {n}\n\n*{state}*\n")
        md.append("| Home | | Away |")
        md.append("|---|:--:|---|")

        for fx in by_gw.get(n, []):
            h, a = fx["home"], fx["away"]
            hs, as_ = scores.get(h, 0), scores.get(a, 0)
            for t, sf, sa in ((h, hs, as_), (a, as_, hs)):
                table[t]["P"] += 1
                table[t]["PF"] += sf
                table[t]["PA"] += sa
                if sf > sa:
                    table[t]["W"] += 1
                    table[t]["Pts"] += WIN
                elif sf == sa:
                    table[t]["D"] += 1
                    table[t]["Pts"] += DRAW
                else:
                    table[t]["L"] += 1

            line = f"  {names[h]:<22} {hs:>3} - {as_:<3} {names[a]:<22}"
            if compare and "actual" in fx:
                ah, aa = fx["actual"]["home"], fx["actual"]["away"]
                deltas += [hs - ah, as_ - aa]
                line += f"      league: {ah:>3} - {aa:<3}"
                ours = "W" if hs > as_ else "D" if hs == as_ else "L"
                theirs = "W" if ah > aa else "D" if ah == aa else "L"
                line += "  ✓" if ours == theirs else f"  ✗ outcome differs ({ours} vs {theirs})"
            print(line)
            md.append(f"| {names[h]} | **{hs} - {as_}** | {names[a]} |")

    print(f"\n{'='*66}\nH2H table\n{'='*66}")
    print(f"{'':>3} {'':<4} {'Team':<24} {'P':>2} {'W':>2} {'D':>2} {'L':>2} {'PF':>5} {'PA':>5} {'Pts':>4}")
    ranked = sorted(table.items(), key=lambda kv: (-kv[1]["Pts"], -(kv[1]["PF"] - kv[1]["PA"]), -kv[1]["PF"]))
    for i, (m, r) in enumerate(ranked, 1):
        print(f"{i:>3} {m:<4} {names[m]:<24} {r['P']:>2} {r['W']:>2} {r['D']:>2} {r['L']:>2} "
              f"{r['PF']:>5} {r['PA']:>5} {r['Pts']:>4}")

    if markdown:
        rows = ["", "## H2H table", "",
                "| # | Team | P | W | D | L | PF | PA | Pts |",
                "|--:|---|--:|--:|--:|--:|--:|--:|--:|"]
        for i, (m, r) in enumerate(ranked, 1):
            rows.append(f"| {i} | {names[m]} | {r['P']} | {r['W']} | {r['D']} | {r['L']} "
                        f"| {r['PF']} | {r['PA']} | **{r['Pts']}** |")
        header = [
            "# Shadow league standings", "",
            "Scored by our own engine from raw FPL match stats, not copied from",
            "FPL's totals. The XI is chosen in hindsight — the best legal 11 from",
            "each squad — so totals run hot for everyone. Comparative, not a replay",
            "of the real season.", "",
            f"*Updated automatically. {len(files)} gameweek(s) scored.*",
        ]
        out = Path(__file__).resolve().parent / "STANDINGS.md"
        out.write_text("\n".join(header + rows + md) + "\n")
        print(f"\nWrote {out.name}")

    if compare and deltas:
        avg = sum(deltas) / len(deltas)
        print(f"\nvs the league's own scores ({len(deltas)} team-gameweeks):")
        print(f"  average difference: {avg:+.1f} points")
        print(f"  range: {min(deltas):+d} to {max(deltas):+d}")
        print(f"  ours lower in {sum(1 for d in deltas if d < 0)} case(s)")
        print("  A hindsight XI should never score less than a real one, so any"
              "\n  negative here means our data is incomplete, not that the engine is wrong.")

    print("\nNote: XI chosen in hindsight — totals run hot for everyone, so this"
          "\ntable is comparative rather than a replay of the real season.")


if __name__ == "__main__":
    main()
