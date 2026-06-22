# CLAUDE.md — Automatic Time Tracker

> Context and working instructions for Claude Code. Read this first, then `TODO.md`
> (the plan) and `FINDINGS.md` (verified technical discoveries — do not re-investigate
> things already settled there).

## What we're building

A local, private automatic time tracker for Saad's Mac (and his iPhone/Apple Watch via
Apple's synced data). It records which app is in the foreground over the day, and — for
the browser (**Comet**, a Chromium-based browser) — which website is open. It stores
everything in one local SQLite database and shows it on a dashboard with:

- total time per app and per website,
- a **timeline** view (a line/area graph of *when* each app/site was used during the day),
- user-defined **categories** (e.g. Research, NEPOMUC, Uni, Leisure, Messaging).

Think "Apple Screen Time, but with real website-level detail for Comet, proper
visualisation, and Mac + iPhone unified in one dashboard."

## Hard constraints / ground truth

- **Usage data stays local.** Never upload the DB/timeline — no cloud, no telemetry. *Soft
  constraint (relaxed by Saad):* fetching public assets (app/site icons, fonts) over the
  network is OK if cached locally; only the lookup key leaves the machine.
- **Tags are the primary (only) manual label, time-aware.** `config/tags.json` holds
  `{type,key,tag,on,since}` assignments set from the dashboard; an app/site/page/dir can carry
  any number of tags. `type` is `app | site | page | dir` (`page` = one subpage's canonical
  URL; `dir` = a Warp project root, cascading to subfolders by longest-prefix). Latest entry
  at/before a ts decides on/off; default `since=0` (retroactive — a tag describes what a thing
  *is*). Resolved by `categorize.Tagger`. (The old per-chunk category + `overrides.json` are
  retired; `overrides.json` is left on disk, unread.)
- **Categories are emergent — named unions of tags.** `config/categories.json` =
  `{categories:[{name,tags}]}`; a category's daily time sums every chunk whose tag set
  *intersects* its tags, so categories OVERLAP and their totals can exceed the day total.
  Derived at query time (`queries.build_summary`); create/edit/delete in dashboard ⚙ →
  Categories (`categorize.save_category_def`/`delete_category_def`). No bundle/domain rules.
- **Tag roles drive the hero rings only.** `settings.json → tag_roles {focus,productive,fun}`
  gives each tag one role (unlisted = Neither). Four rings: Focus / Productive / Fun (each =
  summed time of its role's tags; a chunk counts once per distinct role it carries) + Deep-work
  blocks (uninterrupted Focus-role time). Set manually in ⚙; roles are NOT tags or categories.
- **Dashboard prefs filter at query time, never mutate the DB.** `config/` JSON drives
  `queries.build_summary`: `tags.json` (tags), `categories.json` (category defs), `ignore.json`
  (hidden apps/sites), `timeline.json` (per-view lane prefs — `{tag:{order,shown,hidden},
  category:{…}}`), `settings.json → tag_roles`. All dashboard-written, read fresh per request,
  no restart. Toggles are reversible because raw rows are untouched — add any new view control
  the same way. The Activity timeline has two views (Tags / Categories) that re-lane the same
  sessions; a session carries the UNION of its samples' tags.
- **iPhone data is periodic, not real-time.** It arrives via Apple's cross-device sync
  whenever the phone syncs (minutes-to-hours lag). Mac data is live. This is accepted —
  do not try to build a custom iOS app to get real-time phone data (Apple sandboxes that).
- **Personal sample data must not be committed/kept.** Sample Biome files copied for
  development go in `dev/samples/` and are deleted at the end. Treat them as sensitive.

## Architecture (4 components)

1. **Collector** (`collector/`, Python 3): a background agent that
   - every few seconds records the frontmost macOS app + active window title;
   - if the front app is Comet, asks it for the active tab URL via AppleScript;
   - skips/marks idle time (use macOS idle seconds);
   - periodically decodes Apple's Biome files to import iPhone + Apple Watch app usage;
   - writes everything to the SQLite DB.
2. **Storage** (`data/timetracker.sqlite`): one SQLite file. Unified schema for live Mac
   samples and imported device events. Must not double-count Mac usage that appears in
   both the live sampler and Biome.
3. **Dashboard** (`dashboard/`): a small local web app — a Python server that reads the
   DB and an HTML/JS page (Chart.js) showing totals, tags, emergent categories, the rings,
   and the timeline.
4. **Classification** (`config/tags.json` + `config/categories.json`): dashboard-edited tag
   assignments (the manual layer) and emergent category definitions (named unions of tags).

## Verified facts (already tested on Saad's machine — see FINDINGS.md for detail)

- **Comet is scriptable.** This AppleScript returns the exact, full active-tab URL:
  `osascript -e 'tell application "Comet" to get URL of active tab of front window'`
  Comet bundle id: `ai.perplexity.comet`. It has the full Chromium AppleScript dictionary
  (`tab` objects expose `URL`, `title`). Private/incognito windows hide the URL by design.
- **Mac app usage** is readable from `~/Library/Application Support/Knowledge/knowledgeC.db`
  (stream `/app/usage`, table `ZOBJECT`) and/or Biome `App.InFocus/local`. Timestamps are
  Mac absolute time (add `978307200` for Unix epoch).
- **iPhone + Apple Watch app usage** lives in Apple's **Biome** binary streams at
  `~/Library/Biome/streams/restricted/App.InFocus/remote/<DEVICE-GUID>/`. Files are in
  Apple's binary **SEGB** format (magic `SEGB`). Each remote GUID is one device; identify
  the iPhone by its app signature (iOS-only bundle IDs), NOT by hardcoding a GUID. There
  is also `ScreenTime.AppUsage` (Mac only on this machine) and `App.WebUsage`.
- The classic Screen Time databases (`RMAdminStore-Local.sqlite`, the `ScreenTimeAgent`
  folder) **do not exist** on this macOS build. `RemoteManagement.sqlite` is MDM data and
  is **empty/irrelevant** — do not use it. `knowledgeC.db` holds Mac data only; the iPhone
  is NOT in it as app-usage. **The only source for iPhone usage is the Biome SEGB files.**

## The riskiest piece — do this first

The **SEGB decoder** (Phase 1 in TODO.md) is the only part with real unknowns. Build and
validate it before anything else. Records contain an app bundle ID plus timestamp(s); the
bytes after each bundle-ID string (`J`=0x4A, `(`=0x28, etc.) are protobuf field tags —
the payloads are protobuf inside SEGB framing. Reference existing SEGB/Biome parsers
(e.g. `mac_apt`'s biome plugin) rather than guessing from scratch.

**Validation gate:** the decoder is only "done" when its per-app daily totals roughly
match what Saad sees in his iPhone's **Settings → Screen Time** for the same day. Ask Saad
to confirm a specific number before moving on.

## Environment & permissions

- macOS, June 2026 build. Python 3 available.
- **Full Disk Access** is required for the terminal/app running this code, to read
  `knowledgeC.db` and the `~/Library/Biome` files. (Already appears granted — verify.)
- launchd gotcha: the **background agent's** python3 needs its OWN Full Disk Access grant
  (System Settings → add the python3 binary), separate from the Terminal's — the Biome
  importer fails with `PermissionError` otherwise (the Terminal having access is not enough).
  After granting, restart it: `launchctl kickstart -k gui/$(id -u)/com.saad.timetracker.collector`;
  the importer also logs a loud startup banner when Biome is unreadable.
- The first AppleScript call to Comet may trigger a macOS Automation permission prompt;
  Saad must approve it once.
- **Accessibility** (separate from Full Disk Access) is required by the **collector's** python3
  to read window titles (`AXTitle` via System Events). Without it the AppleScript silently
  degrades to `""` and *every* app's `window_title` is stored NULL — Automation access to Comet
  still yields tab titles, so Comet looks fine while all other apps (incl. Warp) have no title.
  This breaks the Warp per-directory feature (titles carry the cwd). Grant: System Settings →
  Privacy & Security → Accessibility → add the launchd python binary
  (`/opt/homebrew/Cellar/python@3.13/.../Python.app`), then `launchctl kickstart -k …collector`.
- **Warp cwd capture**: a zsh `precmd` hook in `~/.zshrc` writes the cwd into Warp's title
  (`~/proj`); only present at an idle shell prompt — while a program runs it owns the title, so
  those samples bucket as `(other)`. Needs `WARP_HONOR_PS1=1` (Warp default). Claude Code is
  the exception: the collector recovers its cwd from the freshest
  `~/.claude/projects/<slug>/*.jsonl` log (`appinfo.claude_active_dir`), mapping
  Claude-in-Warp time to its directory not `(other)`. That log isn't written while Claude
  waits on a permission prompt / long tool, so a Claude-signature title (`✳`/braille
  spinner/`Wants to run`, via `appinfo.is_claude_title`) widens the freshness window to
  30 min; other titles keep the ≤60s fallback.
- Unlike the Cowork session this plan was made in, **Claude Code can run code directly on
  this Mac** — use that to develop/test/iterate the collector and decoder yourself instead
  of asking Saad to paste output. Only ask Saad for: granting permissions, the iPhone
  Screen Time validation check, category preferences, and final install approval.
- **Restart the dashboard server after editing `dashboard/*.py`** — `server.py` caches
  imported modules in memory, so stale code yields wrong output. Two instances run: 8765, 8766.
- **`mdfind` does not index app bundle ids on this Mac** — the icon service finds Mac apps by
  scanning `/Applications` (+ system/user app dirs) and reading each `Info.plist`, not Spotlight.
- **Playwright (dev-only):** `full_page` screenshots render Chart.js canvases blank — verify
  charts via element screenshots or a `toDataURL` non-white-pixel count.

## Tech choices (keep it simple)

- Python 3 standard library where possible. SQLite via `sqlite3`. Avoid heavy frameworks.
- Dashboard: plain HTML + JS + Chart.js served by a minimal Python `http.server`. No build
  step, no npm.
- Background run: a `launchd` LaunchAgent (`~/Library/LaunchAgents/`) so the collector
  starts at login. Provide a clean uninstall/stop script too.

## Working agreements (Saad's preferences — follow these)

- **Confirm before overwriting/deleting/renaming** any existing file; show what will change.
- **Surgical changes**: touch only what the task needs; match existing style; don't refactor
  unrelated code; don't add speculative features or abstractions.
- **Plan, then act**: for multi-step work, outline briefly and proceed; summarise after each
  phase what was done and what's next.
- **Name new files** `YYYY-MM-DD-descriptive-name` where practical. (Exceptions: this
  `CLAUDE.md`, `TODO.md`, and `FINDINGS.md` keep standard names so they're auto-discovered.)
- **At the end of a task, list all files created or modified, with their locations.**
- Be concise and direct.

## Suggested layout

```
Time Tracker/
  CLAUDE.md          # this file
  TODO.md            # the plan / checklist
  FINDINGS.md        # verified technical discoveries + reference scripts
  collector/         # background agent (Mac sampler + Comet URL + Biome importer)
  decoder/           # SEGB / Biome decoder (build & validate first)
  dashboard/         # local web server + HTML/JS dashboard
  config/            # tags, emergent categories, tag_roles, ignore, timeline prefs
  data/              # timetracker.sqlite (gitignored)
  dev/samples/       # copied sample Biome files for development (sensitive; delete later)
```
