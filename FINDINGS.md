# FINDINGS.md — Verified technical discoveries

Everything here was tested on Saad's actual Mac on 2026-06-15. Trust it; don't re-derive
the dead-ends. Paths assume the user `saadalam`.

---

## 1. Comet browser — active URL is fully readable ✅

Comet (Perplexity's Chromium-based browser, bundle `ai.perplexity.comet`) ships the full
Chromium AppleScript dictionary. Confirmed via Script Editor → Open Dictionary: it has a
**Chromium Suite** with a `tab` class exposing:

- `URL` (text) — "the url visible to the user"
- `title` (text), `id`, `loading`

A `window` contains `tabs`; one is the active tab. **Working command (tested):**

```bash
osascript -e 'tell application "Comet" to get URL of active tab of front window'
# → https://www.espn.com/soccer/story/_/id/49011009/...   (real full URL returned)
```

Notes:
- Use this on a timer in the collector when the frontmost app is Comet.
- **Private/incognito windows**: Chromium blocks URL access by design → record the app as
  active but URL unknown (label e.g. "Comet — private").
- First call may trigger a macOS Automation-permission prompt (one-time, user approves).
- Same pattern works for Google Chrome (also installed) if ever needed.
- **Page titles**: Comet's front-window AXTitle comes back empty, so the only reliable
  source for per-page labels (e.g. YouTube video names) is the tab's own property:
  `get title of active tab of front window`. The collector fetches URL + title together.
- **YouTube is a single-page app**: the active-tab URL sometimes lags as the bare host
  (`youtube.com/`) while the title is already the video → one video splits into two page
  keys. `queries.build_summary` merges subpages sharing the same title, keeping the most
  specific (`watch?v=`) key.

## 2. Mac app usage — `knowledgeC.db` ✅ (local device only)

Path: `~/Library/Application Support/Knowledge/knowledgeC.db` (SQLite, protected → needs
Full Disk Access). Confirmed readable.

- App usage: table `ZOBJECT`, where `ZSTREAMNAME = '/app/usage'`. `ZVALUESTRING` = bundle
  id, duration = `ZENDDATE - ZSTARTDATE`.
- Timestamps are **Mac absolute time** (CFAbsoluteTime): add `978307200` to get Unix epoch.
- Streams present include `/app/usage`, `/app/webUsage`, `/app/intents`, `/app/mediaUsage`,
  `/notification/usage`, plus `/knowledge-sync-*` bookmarks.
- Open read-only safely:
  ```python
  from urllib.request import pathname2url
  uri = "file:" + pathname2url(path) + "?mode=ro&immutable=1"
  con = sqlite3.connect(uri, uri=True)
  ```

**Important:** `knowledgeC.db` on this build contains **only this Mac's** apps. A non-local
device GUID (`088ECA9A-B564-5B9B-8FAC-3300DD72FE24`) appears in `ZSOURCE`, but only tied to
sync-bookmark streams — **not** to `/app/usage`. So `knowledgeC.db` is NOT a source of
iPhone usage. (The live macOS sampler is the primary Mac source anyway; knowledgeC is
useful only for backfilling history.)

Last-7-day Mac totals observed (sanity reference): Comet ~31.8 h, Claude desktop ~13.4 h,
Warp ~2.7 h, Obsidian ~0.6 h, etc.

## 3. iPhone & Apple Watch app usage — Biome SEGB files ✅ (THE source for phone)

Path: `~/Library/Biome/streams/restricted/`

Relevant streams (each is a directory containing `local/`, sometimes `remote/`, a `lock`
file, and `tombstone/` subdirs):

- **`App.InFocus`** — has BOTH `local/` (this Mac) and `remote/<GUID>/` (synced devices).
  **This is the iPhone source.** Records when each app is in the foreground → gives both
  totals and the timeline.
- `ScreenTime.AppUsage` — `local/` only on this machine (Mac usage; per-app screen time).
- `App.WebUsage` — `local/` only (just Safari observed). No iPhone web data → fine, Saad
  doesn't browse on the phone.
- `App.MediaUsage` — `local/` only.

### Devices observed under `App.InFocus/remote/`

Identify devices by app signature, **don't hardcode GUIDs** (they can change):

- `C7426CE5-5C31-416D-8950-95077A568D36` → **iPhone** (TikTok `com.zhiliaoapp.musically`,
  `net.whatsapp.WhatsApp`, `com.burbn.instagram`, `com.apple.mobilesafari`,
  `com.spotify.client`, `com.google.gemini`, `com.mobilefootie.fotmobpro`,
  `com.ubercab.UberEats`, `com.google.ios.youtube`, `com.apple.mobilemail`, etc.)
- `36AE39DC-4762-4C2D-943D-5FEA16DA2395` → **Apple Watch** (`com.apple.carousel.*`,
  `com.apple.NanoSleep.watchkitapp`, `com.apple.HeartRate`, etc.)
- Other GUIDs present were empty or stale (only tombstones).

### SEGB file format notes (for the decoder)

- Files are fixed-size segments: data segments are **524288 bytes (512 KB)**; `tombstone/`
  files are 1 MB and represent deletions (can likely be ignored for usage).
- Magic header: first 4 bytes `b'SEGB'`, then 4 more bytes (varies per file, e.g.
  `\x14\x04\x00\x00`).
- A `lock` file (0 bytes) sits in each stream dir — skip it.
- Inside, records are **protobuf** wrapped in SEGB framing. App bundle IDs appear as
  readable strings; the byte immediately after a bundle-ID string is the next protobuf
  field tag — observed `J` (0x4A = field 9, wire type 2) in `App.InFocus`, `(` (0x28) in
  `ScreenTime.AppUsage`, `@` (0x40) in `App.WebUsage`. Use a generic protobuf wire-format
  walk to pull string fields (bundle id) and varint/double fields (timestamps).
- Timestamps are expected to be Mac absolute time (CFAbsoluteTime, +978307200). Sanity-check
  decoded times land on recent real dates.
- Reference implementations to consult (don't reinvent): `mac_apt` biome plugin
  (github.com/ydkhatri/mac_apt) and public SEGB v1/v2 format write-ups.

### Decoder strategy — SOLVED & VALIDATED (2026-06-20)

Implemented in `decoder/segb.py`; numbers confirmed against iPhone Screen Time.

1. **SEGB framing** (verified): 32-byte header (`SEGB` magic, uint32 record count at +4,
   creation-time double at +8). Record payloads are laid out from byte 32. A **metadata
   table grows backwards from EOF** in 16-byte entries `[end_offset uint32][state uint32]
   [timestamp double]`; reading from EOF yields records in chronological order, `end_offset`
   is cumulative (relative to byte 32) so consecutive offsets delimit each record, `state`=1
   means live (skip others). The metadata `timestamp` is the reliable event time and equals
   protobuf field 4.
2. **Record protobuf**: a short, variable-length alignment sub-header precedes a protobuf
   message that reliably starts `10 .. 18` (field 2 then field 3) — search for that to skip
   the sub-header. Read varint **lengths** properly (bundle strings push field lengths > 127
   in some records). Fields: f2 varint (≈1), **f3 varint = focus flag**, f4 double = event
   time, **f6 string = bundle id**, f9/f10 = version/build, f13 varint.
3. **Durations via start/end pairing — this is the key correctness fact.** Each `App.InFocus`
   record is *either* a focus **start** (`f3 == 1`, app gained foreground) *or* a focus
   **end** (`f3 == 0`, app lost foreground). Pair each start with the next matching end for
   the same bundle id → the exact foreground session. Sum sessions per app/day; clip to the
   day; keep sessions for the timeline. Drop pathological unpaired sessions longer than
   `max_session_seconds` (config). **Do NOT** derive durations by gap-filling between
   consecutive events — that massively over-counts (it attributes phone-down time to the
   last app). The earlier gap-fill idea (now removed) gave TikTok ~2× and other apps ~3×.
4. Auto-detect which `remote/<GUID>` is the iPhone vs Watch by app signature
   (`collector/appinfo.classify_device`), never by hardcoded GUID.
5. **Validation result (06-20):** decoded Instagram 37m / Claude 32m / Kicktipp 16m matched
   Screen Time exactly; TikTok 303m vs 328m and total 7.7h vs 8h10m (gap = day still in
   progress + sessions not yet ended/synced). Good enough — gate passed.

## 4. Dead ends — do NOT pursue (already ruled out)

- `~/Library/Application Support/com.apple.RemoteManagementAgent/Database/RemoteManagement.sqlite`
  — MDM/device-management data; all usage-ish tables are **empty**. Not Screen Time.
- `RMAdminStore-Local.sqlite` / `~/Library/Application Support/com.apple.ScreenTimeAgent/`
  — **do not exist** on this macOS build.
- `~/Library/Containers/com.apple.UsageTrackingAgent/.../UsageTracking.sqlite` — contains
  only `ZDEVICEACTIVITYBUDGET` / `ZREGISTEREDDEVICEACTIVITYEVENT` (Screen Time limits/
  schedules), **not** usage minutes.
- Building a custom iOS app for real-time phone data — blocked by Apple's DeviceActivity
  sandboxing; not worth it.

## 5. Reference scripts (verified working)

### Read Mac app usage from knowledgeC.db
```python
import sqlite3, os, time, collections
from urllib.request import pathname2url
db = os.path.expanduser('~/Library/Application Support/Knowledge/knowledgeC.db')
con = sqlite3.connect("file:"+pathname2url(db)+"?mode=ro&immutable=1", uri=True)
cur = con.cursor()
MAC_EPOCH = 978307200
cutoff = time.time() - MAC_EPOCH - 7*86400
rows = cur.execute("""
  SELECT s.ZDEVICEID, o.ZVALUESTRING, (o.ZENDDATE - o.ZSTARTDATE)
  FROM ZOBJECT o LEFT JOIN ZSOURCE s ON o.ZSOURCE = s.Z_PK
  WHERE o.ZSTREAMNAME = '/app/usage' AND o.ZSTARTDATE > ?
""", (cutoff,)).fetchall()
```

### List Biome streams and read raw SEGB files (dev: copy to a folder first)
```python
import os, re, collections
root = os.path.expanduser('~/Library/Biome/streams/restricted/App.InFocus')
for dp,_,fs in os.walk(root):
    for fn in fs:
        if fn == 'lock': continue
        data = open(os.path.join(dp,fn),'rb').read()
        # data[:4] == b'SEGB'; strings reveal bundle ids:
        strs = re.findall(rb'[\x20-\x7e]{3,}', data)
        # bundle ids look like com.* net.* org.* ai.* io.* etc.
```

### Comet active URL (run on the Mac)
```bash
osascript -e 'tell application "Comet" to get URL of active tab of front window'
```

## 6. Recommended dev workflow in Claude Code

Because Claude Code runs on the Mac directly, develop the decoder against the **live**
Biome files (read-only) or against copies in `dev/samples/`. Iterate freely — no need to
ask Saad to paste output. Save Saad's involvement for: permission grants, the Screen Time
validation check, category choices, and the final `launchd` install.
