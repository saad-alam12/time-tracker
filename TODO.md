# TODO — Automatic Time Tracker build plan

Build order is deliberate: **Phase 1 (the SEGB decoder) is the only risky part — prove it
first.** Everything after is low-risk, mostly one-shot work. Check items off as you go.

Legend: 🔵 = needs Saad's input/action · ⚙️ = Claude Code can do solo on the Mac.

---

## Phase 0 — Setup ⚙️ / 🔵 (one-time)
- [ ] Confirm Full Disk Access for the terminal running Claude Code (needed for
      `knowledgeC.db` + `~/Library/Biome`). 🔵 if not already granted.
- [ ] Create folder layout (`collector/ decoder/ dashboard/ config/ data/ dev/samples/`).
- [ ] Add a `.gitignore` (ignore `data/`, `dev/samples/`, `*.sqlite`).
- [ ] (Optional) Copy a couple of `App.InFocus/remote/<iPhone-GUID>/*` SEGB files into
      `dev/samples/` for offline iteration. Delete before finishing.

## Phase 1 — iPhone/Watch SEGB decoder ⚙️ + 🔵 validation  ← DO FIRST (riskiest)
- [ ] **1a.** Parse SEGB framing: split a `.segb` segment into individual records with
      timestamps + raw payloads. Verify record count and timestamps look sane.
- [ ] **1b.** Protobuf-decode each record → bundle id + event time(s). (May take 1–2 tries.)
- [ ] **1c.** Convert focus events → per-app durations + per-day totals; map bundle IDs to
      friendly names (TikTok, WhatsApp, …). Auto-detect iPhone vs Watch by app signature.
- [ ] **1d.** 🔵 **Validation gate:** compute one day's totals, ask Saad to compare against
      his iPhone Settings → Screen Time. Must roughly match before proceeding.

## Phase 2 — Mac live collector ⚙️ (+ one-time 🔵 Automation prompt)
- [ ] **2a.** Frontmost-app sampler (app + window title) on a timer; idle detection.
- [ ] **2b.** When front app is Comet, fetch active-tab URL via AppleScript; handle
      private windows. 🔵 approve the one-time Automation prompt.
- [ ] **2c.** Write samples to SQLite. Run it live a few minutes; confirm rows look right.
- [ ] **2d.** (Optional) Backfill Mac history from `knowledgeC.db` so day one isn't empty.

## Phase 3 — Unified database ⚙️
- [ ] Finalise one SQLite schema covering: live Mac samples, Comet URLs, and imported
      device (iPhone/Watch) events. Tag every row with source + device.
- [ ] Ensure Mac usage isn't double-counted (live sampler vs Biome). Add indexes for
      per-day / per-app / timeline queries.

## Phase 4 — Dashboard ⚙️ + 🔵 design feedback
- [ ] **4a.** Minimal Python server that reads the DB and serves JSON.
- [ ] **4b.** HTML/JS page (Chart.js): totals per app + per website, category breakdown,
      and the timeline graph (when each app/site was used). Mac + iPhone unified.
- [ ] **4c.** 🔵 Saad reviews look/feel; iterate.

## Phase 5 — Categories ⚙️ + 🔵 buckets
- [ ] 🔵 Saad provides his groupings (e.g. Research / NEPOMUC / Uni / Leisure / Messaging).
- [ ] Write editable rules (`config/categories.*`) mapping bundle IDs + domains → category;
      apply across Mac and iPhone.

## Phase 6 — Auto-start ⚙️ + 🔵 install
- [ ] Create a `launchd` LaunchAgent so the collector starts at login and runs quietly.
- [ ] Provide start/stop/uninstall scripts.
- [ ] 🔵 Saad runs the install (or approves Claude Code running it).

## Phase 7 — End-to-end verification ⚙️ + 🔵 observe
- [ ] 🔵 Let it run ~a day normally.
- [ ] Confirm dashboard shows Mac + iPhone correctly; check totals against Screen Time.
- [ ] Handle edge cases: private browsing, sleep/lid-close gaps, iPhone sync lag, timezone.
- [ ] 🔵 Delete `dev/samples/` (sensitive personal data).

---

## Notes
- In Claude Code (running on the Mac), the dev loop is much faster than the Cowork session
  where this plan was written: run/test/iterate the collector and decoder yourself; only
  pull Saad in at the 🔵 points above.
- Keep it simple — stdlib Python, SQLite, plain HTML/JS + Chart.js, no build step.
- Privacy first: all data local; never upload; remove sample data at the end.
- See `FINDINGS.md` for exact paths, the SEGB format, verified scripts, and ruled-out
  dead ends. See `CLAUDE.md` for architecture, constraints, and working agreements.

## Open decisions to confirm with Saad when reached
- Polling interval for the live sampler (e.g. 2–5 s)?
- Idle threshold (stop counting after N seconds of no input)?
- How long to retain raw timeline data vs daily aggregates?
- Category buckets (Phase 5).

---

## Phase 8 — Dashboard improvements (beat Apple Screen Time)

Status: backend done, single-day dashboard works. These add things Screen Time can't do,
tuned for Saad (heavy Comet + TikTok, Mac+iPhone, custom categories). Build top-down by
value; each is independent.

### 8A. Things Screen Time literally cannot show (highest value)
- [ ] **Website breakdown for Comet.** Per-site time (already have domains), plus split
      *research-browsing vs leisure-browsing* by domain category. Screen Time shows only
      one lump "Comet". This is the killer feature.
- [ ] **Focus score per day.** One number: % of tracked time in productive categories
      (Research/NEPOMUC/Uni/Dev) vs Leisure/Messaging. Uses *your* buckets, which Screen
      Time has no concept of.
- [ ] **Deep-work blocks.** Detect longest uninterrupted productive stretches on the Mac
      (e.g. "1h40m in Warp+Claude, 14:05–15:45"). Useful for research focus; Screen Time
      can't do sessions.
- [ ] **Hourly pattern ("when do you doomscroll").** Average usage by hour-of-day across
      many days → see that TikTok peaks 22:00–00:00, research peaks mornings, etc.
- [ ] **Today vs your average.** Context line: "TikTok 303m, +120m above your 14-day avg."

### 8B. See trends over time, not just one day
- [ ] **Date-range picker**: day / week / month, not only single day.
- [ ] **Calendar heatmap** (GitHub-style): one square per day, colour = total time or
      leisure %. Spot patterns across a month at a glance.
- [ ] **Per-category trend lines** over weeks (e.g. Research hours/day).
- [ ] **Search one app/site across time** ("all my arxiv.org time this month").

### 8C. Prettier / nicer to use
- [ ] App icons next to app names; website favicons next to domains.
- [ ] Consistent colour per category everywhere; light/dark toggle; nicer typography.
- [ ] Richer hover tooltips (exact h:m:s); small sparklines on each app row.
- [ ] Show idle/away time separately so totals aren't inflated.

### 8D. Extras
- [ ] **Local weekly summary** written as a markdown file in the folder (no upload) —
      "this week: 14h research, 9h TikTok, best focus day Tue."
- [ ] Optional soft goals (e.g. "TikTok < 1h/day") shown as progress, not enforced.

Decisions for Saad: which of 8A to do first; what counts as "productive" for the focus
score; soft-goal targets if wanted.
