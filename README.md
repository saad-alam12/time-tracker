# Time Tracker

A local, private automatic time tracker for macOS + iPhone/Apple Watch. It records which
app is in the foreground (and, for the **Comet** browser, which website), stores everything
in one local SQLite database, and shows it on a dashboard with totals, categories, and a
timeline. **Nothing is ever uploaded.**

## Components

| Path                 | What it does                                                            |
|----------------------|-------------------------------------------------------------------------|
| `decoder/segb.py`    | Decodes Apple Biome **SEGB** files → iPhone/Watch app-focus events.     |
| `collector/`         | Background agent: Mac live sampler + Comet URL + Biome importer.         |
| `dashboard/`         | Local web server (`http.server`) + Chart.js page. Reads the DB only.    |
| `config/`            | `settings.json` (collector tuning) and `categories.json` (your buckets).|
| `data/`              | `timetracker.sqlite` — your data (git-ignored, never leaves the Mac).   |
| `scripts/`           | install / uninstall / dashboard launchers.                              |

## Data sources (no double-counting)

- **Mac** usage comes from the live sampler (`collector/sampler.py`) — one row per poll.
- **iPhone / Apple Watch** usage comes from Apple's Biome `App.InFocus/remote/<device>`
  streams (`collector/importer.py`), decoded by `decoder/segb.py`. Only *synced* devices
  are imported — the Mac's own Biome stream is never read — so the two never overlap.
- iPhone data is **periodic**, not live: it appears whenever the phone syncs to the Mac
  (minutes-to-hours lag). This is expected.

## Run it

```bash
# 1. View the dashboard (starts a local server, opens the browser):
scripts/dashboard.sh            # → http://127.0.0.1:8765

# 2. Run the collector in the background at login:
scripts/install.sh              # installs a launchd LaunchAgent
scripts/uninstall.sh            # stop + remove it (keeps your data)
```

### Permissions (one-time)
- **Full Disk Access** for the python3 that runs the collector (reads `knowledgeC.db` /
  Biome). See the note printed by `scripts/install.sh`.
- **Automation** prompt the first time the sampler asks Comet for its URL — approve it.

## Configuration
- `config/settings.json` — poll interval, idle threshold, the gap cap for phone events.
- `config/categories.json` — map app bundle ids and website domains to your categories
  (Research / NEPOMUC / Uni / Dev / Messaging / Leisure …). Edited live; no restart needed
  for the dashboard.
