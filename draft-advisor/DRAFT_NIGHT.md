# Draft night run-book

Everything you do on the night, in order. Setup takes ~10 minutes. Do a
dry run of these steps the day before.

Topology: **your laptop runs both apps.** The auction app (port 3001) is
what the room connects to. The advisor (port 8099) is private to you.

```
            your laptop
   ┌───────────────────────────────┐
   │  auction server  :3001  ───────┼──►  room (phones)   http://<your-ip>:3001/
   │      │  (live FPL data)        │     you, auctioneer http://<your-ip>:3001/admin
   │      ▼ socket                  │
   │  advisor  :8099  (private)     │     you only        http://localhost:8099/advisor.html
   └───────────────────────────────┘
```

## The day before
1. **Refresh the data.** The FPL dataset re-bakes daily via GitHub Actions.
   Pull the latest so the advisor has current prices/injuries:
   ```
   cd omtffl && git pull origin claude/fpl-draft-tool-scope-nkw048
   ```
2. **Full dry run** of the steps below, using the advisor's **demo** button
   (replays a past draft) so the moves are muscle memory.

## Setup (≈10 min before the draft)

### 1. Start the auction app
```
cd exec-search/fpl-draft
npm install          # first time only
cd client && npm install && npm run build && cd ..
npm start            # serves everything on http://0.0.0.0:3001
```
Wait for `Loaded NNN players.` — that confirms it pulled live FPL data
(your laptop needs internet; this is the one part that does).

### 2. Find your laptop's IP
- **Mac**: `ipconfig getifaddr en0`
- **Windows**: `ipconfig` → IPv4 Address
- **Linux**: `hostname -I`

Call it `<your-ip>` (e.g. 192.168.1.42).

### 3. Share with the room
- Everyone (view only): **`http://<your-ip>:3001/`**
- You, the auctioneer: **`http://<your-ip>:3001/admin`** (PIN `fpl2025`)

Everyone must be on the **same WiFi**. If phones can't connect, allow port
3001 through your laptop's firewall.

### 4. Start your advisor (private)
In a second terminal:
```
cd omtffl/draft-advisor
python3 -m http.server 8099
```
Open **`http://localhost:8099/advisor.html`** on your laptop. Keep it on a
screen only you can see.

### 5. Wire the advisor to the auction
1. In the advisor header, set the server URL to **`http://localhost:3001`**
   and hit **connect** — the dot should go green ("live").
2. Open the **Teams** tab and map each auction team to its manager (it
   auto-maps by name where it can) so rival predictions use their history.
3. Set **"I am team…"** in the header to your own team.

## During the draft
- A player is nominated → the **factsheet appears automatically** with the
  fair range, the verdict, and who else is likely bidding.
- As bids climb, watch the verdict flip **VALUE → FAIR → OVERPRICED** and
  the red marker cross the green fair band. Walk when it says walk.
- Between lots, work the **Gems** tab (best remaining value) and glance at
  **Plan** (your budget shape vs the winners — it'll nudge you off the old
  midfield-heavy habit and toward the cheap productive defenders).
- Know something the data doesn't (a transfer, a nailed-on new starter)?
  Click the **projected-points number** on the factsheet and type your own
  — everything recomputes.

## If something breaks
- **Advisor dot goes red / "lost":** the auction server dropped. Restart
  `npm start`, then the advisor reconnects on its own. **But the auction
  server keeps its state in memory — a restart loses the whole draft.** So:
  don't let the laptop sleep, keep it plugged in, and don't Ctrl-C the
  server. (Worth hardening — see below.)
- **Advisor won't connect:** confirm the URL is `http://localhost:3001`
  (not 8099), and that the auction server terminal shows it's running.
- **A player shows no history / "new to PL":** expected for new signings —
  use the manual override to set your own projection.
- **Whole advisor looks blank:** you opened `advisor.html` as a file
  instead of via `http://localhost:8099` — it needs the local server to
  load its data.

## Recommended hardening (optional, before the night)
The auction server holds the entire draft in memory, so any crash or laptop
sleep wipes it. A ~20-line change to snapshot state to disk on every sale
(and reload on boot) removes that risk. Ask and I'll add it to the auction
app — it's the one thing I'd fix before trusting a live draft to it.
