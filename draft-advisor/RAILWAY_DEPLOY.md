# Putting the auction app on Railway (the simple hosted option)

This gives you **one permanent web link** for the auction app that everyone —
remote and in the room — uses. You do this **once**, and there's nothing to
run on your laptop on the night except your own private advisor.

Cost: Railway's Hobby plan is ~$5/month. Cancel it after the draft.

---

## Part A — Deploy it (once, ~10 minutes)

1. Go to **railway.com** and click **Login → Login with GitHub**. Approve
   the access it asks for.
2. Click **New Project → Deploy from GitHub repo**.
3. Pick the **`exec-search`** repo. (If Railway can't see it, click
   "Configure GitHub App" and give it access to that repo, then come back.)
4. Railway starts a project. Open it, click the service (the box it created),
   then **Settings**, and set two things:
   - **Root Directory**: `fpl-draft`
   - **Branch**: `claude/auction-state-persistence`
     *(this branch has the crash-recovery. If you've already merged it into
     your main branch, use main instead.)*
5. Still in Settings, find **Networking → Public Networking** and click
   **Generate Domain**. Railway gives you a link like
   **`https://fpl-draft-production.up.railway.app`** — **this is your link.**
6. Railway builds and starts the app (watch the **Deployments** tab; first
   build takes a few minutes). When it shows **Active/green**, open your link
   in a browser — you should see the auction app.

That's it. The link stays live 24/7 until you delete the project.

## Part B — On draft night

1. **Share your Railway link with everyone**: `https://<your-link>/`
   Remote and in-room drafters all use it. (Change the admin PIN first —
   see below — and keep `https://<your-link>/admin` to yourself.)
2. **Start your private advisor** on your own laptop:
   ```
   cd omtffl/draft-advisor
   python3 -m http.server 8099
   ```
   Open `http://localhost:8099/advisor.html`.
3. In the advisor header, set the server URL to your **Railway link**
   (`https://<your-link>`, not localhost this time) and hit **connect** —
   the dot goes green. Then map teams and set "I am team…" as usual.

Everything else in `DRAFT_NIGHT.md` (during-the-draft workflow) is the same.

---

## Two things to do before you go public

- **Change the admin PIN.** The default is `2025`
  (`const ADMIN_PIN = '2025'` in
  `exec-search/fpl-draft/client/src/pages/AdminPage.jsx`). Change it and push
  the change to the branch Railway is deploying — Railway auto-rebuilds. This
  matters because your `/admin` page is now on the public internet.

- **(Optional) Make the saved draft bulletproof.** The app already reloads
  the draft if it restarts, which covers you for a single evening. If you want
  it to survive even Railway rebuilding the container: in the service, add a
  **Volume** mounted at `/data`, then add a **Variable** `STATE_DIR` = `/data`.
  The app will then keep the draft on that permanent storage. (Skip this if
  you'd rather not fiddle — the default is fine for one night.)

## If it won't build
- Make sure **Root Directory** is `fpl-draft` (not blank, not `exec-search`).
- Check the **Deployments → build logs**; the build runs
  `npm install && npm run build` and start runs `npm start`. If the log ends
  with `FPL Draft server running…` and `Loaded NNN players.`, it worked.
