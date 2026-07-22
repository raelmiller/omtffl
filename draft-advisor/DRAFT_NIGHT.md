# Draft night run-book

Everything you do on the night, in order. Setup takes ~10 minutes. Do a
dry run of these steps the day before.

Topology: **your laptop runs everything.** A free Cloudflare tunnel gives
one public link that both the remote drafters and the people in the room
use. The app and its saved state stay on your laptop's permanent disk, so
the crash-recovery works (a hosted server's disk is wiped on restart).

```
                 your laptop
   ┌──────────────────────────────────────┐        one public link
   │  auction server  :3001               │        for EVERYONE:
   │      │  (live FPL data + saved state) │   ┌─►  remote drafters
   │      ▼ socket                         │   │    room, on their phones
   │  cloudflared tunnel  ────────────────┼───┘    https://xxxx.trycloudflare.com/
   │      ▲                                │
   │  advisor  :8099  (private, you only)  │        you: http://localhost:8099/advisor.html
   └──────────────────────────────────────┘        auctioneer: <link>/admin
```

Everyone — remote and in-room — opens the **same public tunnel link**. Your
private advisor still points at `http://localhost:3001` (same laptop, no
tunnel needed for you).

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

### 2. Open the public tunnel (for the remote drafters + everyone else)
Install `cloudflared` once, then start a quick tunnel to the auction app:
```
# install (one time):
#   Mac:      brew install cloudflared
#   Windows:  winget install --id Cloudflare.cloudflared
#   Linux:    see https://pkg.cloudflare.com/ (or download the binary)

cloudflared tunnel --url http://localhost:3001
```
It prints a line like `https://random-words-1234.trycloudflare.com` — that
is your **public link**. No account, no time limit, WebSockets work, and it
stays up as long as the command runs. Keep this terminal open.

> Test it before the draft: open the printed URL on your **phone over mobile
> data** (not WiFi). If the auction app loads, remote drafters are good.

### 3. Share the one link with everyone
- Everyone (view only): **`https://<tunnel>.trycloudflare.com/`**
- You, the auctioneer only: **`https://<tunnel>.trycloudflare.com/admin`**

Remote and in-room drafters all use the same link — the room doesn't need to
be on your WiFi. **Before you go public, change the admin PIN** from the
default `2025` (see hardening below) and don't paste the `/admin` link
into the group chat — only you need it.

*(All in the same room and no remote callers? You can skip the tunnel and
just share `http://<your-laptop-ip>:3001/` over WiFi instead — find the IP
with `ipconfig getifaddr en0` / `ipconfig` / `hostname -I`.)*

### 4. Open your advisor (private)
**The easy way — just double-click a file:**
Download **`draft-advisor/advisor-standalone.html`** from the repo (open it
on GitHub → the download/raw button) and **double-click it**. It opens in
your browser with all the data built in — no terminal, no server. Keep it on
a screen only you can see. (This file refreshes automatically each day, so
grab the latest the day before the draft.)

*(Alternative, if you prefer: `cd omtffl/draft-advisor && python3 -m http.server 8099`,
then open `http://localhost:8099/advisor.html`. Same tool, just served
instead of standalone.)*

### 5. Wire the advisor to the auction
1. In the advisor header the server URL is already your Railway link — just
   hit **connect**; the dot should go green ("live").
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
- **Auction server crashes / laptop slept:** just run `npm start` again. The
  draft is **saved to disk after every change and reloads on boot**, so it
  resumes exactly where it left off (teams, budgets, sold players). Drafters
  refresh their page and reconnect. Still: keep the laptop plugged in and
  awake so it doesn't come to that.
- **Tunnel link stops working:** the `cloudflared` terminal was closed or the
  laptop's internet dropped. Re-run `cloudflared tunnel --url http://localhost:3001`
  — note it prints a **new** URL, so reshare it. (Your local advisor is
  unaffected; it uses localhost.)
- **Advisor dot goes red / "lost":** the auction server dropped — restart it
  as above and the advisor reconnects on its own.
- **Advisor won't connect:** confirm the URL is `http://localhost:3001`
  (not the tunnel, not 8099), and that the auction server terminal is running.
- **A player shows no history / "new to PL":** expected for new signings —
  use the manual override to set your own projection.
- **Whole advisor looks blank:** you opened `advisor.html` as a file
  instead of via `http://localhost:8099` — it needs the local server to
  load its data.

## Before you go public (do these once)
- **Crash-recovery is built in.** The auction server now snapshots the draft
  to `server/draft-state.json` on every change and restores it on restart, so
  a crash or a slept laptop no longer wipes the draft. (Delete that file to
  start a fresh draft, or use the app's Reset.)
- **Change the admin PIN.** The default is `2025`
  (`const ADMIN_PIN = '2025'` in
  `exec-search/fpl-draft/client/src/pages/AdminPage.jsx`) — public in the repo,
  and your `/admin` link will be reachable over the tunnel. Change it, then
  rebuild the client (`cd client && npm run build`). Share `/admin` with no
  one but yourself.
