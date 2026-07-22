# Draft history data notes

Source: three league workbooks (2023-24, 2024-25, 2025-26), parsed by
`scripts/ingest_draft_history.py` into `draft_history.csv` / `.json`.

## Season shapes

| Season | Teams | Sales | Budget pool | Recorded spend |
|---|---|---|---|---|
| 2023-24 | 13 | 195 | £650m | £592.75m |
| 2024-25 | 14 | 210 | £700m | £685.75m |
| 2025-26 | 16 | 240 | £800m | £794.75m |

## Known anomalies (recorded as-is, flagged for modelling)

- **Ali 2023-24: every cost is £0.** Prices were never entered in the
  source sheet. Exclude these 15 rows from price training; the squad
  composition is still usable.
- **2023-24 squad shapes vary** (e.g. 1 GK / 4 FWD, 1 GK / 6 MID). Either
  looser rules that year or position typos in the sheet. 2024-25 and
  2025-26 are uniformly 2 GK / 5 DEF / 5 MID / 3 FWD.
- **£0 costs elsewhere are real**: unsold-then-free players / end-of-draft
  fills went for £0. Only Ali's block is a recording gap.
- **Club codes are inconsistent** across seasons (BHA vs BRI, BRE vs BRF,
  a few blanks and typos like Isak's club recorded as "FOR" in 2025-26).
  Treat club as advisory; player name + season is the join key to FPL data.
- **Player names are informal** (surnames, nicknames, misspellings —
  "Soudawara", "Guesson", "Hermansson"). Joining to FPL ids needs fuzzy
  matching with a manual override map.

## Nomination rules (from Rael)

One person nominates a player and a starting value; open auction from
there. No fixed nomination order constraint worth modelling yet.
