#!/usr/bin/env python3
"""Import iPhone / Apple Watch app-focus events from Apple Biome into the database.

Walks ``~/Library/Biome/streams/restricted/App.InFocus/remote/<GUID>/``, decodes each
SEGB segment with the project's decoder, classifies every device as iPhone/Watch by its
app signature (never by hardcoded GUID), and inserts the focus events. Re-running is safe:
``device_events`` has a UNIQUE(device, ts, bundle_id) constraint, so already-seen events
are ignored and only new ones are added.

Mac usage is intentionally NOT imported here — only the synced ``remote/`` devices — so it
can't double-count the live sampler.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Make the sibling decoder package importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "decoder"))

from appinfo import classify_device  # noqa: E402
from db import connect, insert_device_events  # noqa: E402
from segb import parse_device_dir  # noqa: E402

BIOME_REMOTE = (
    Path.home() / "Library/Biome/streams/restricted/App.InFocus/remote"
)


def biome_access_ok() -> tuple[bool, str]:
    """Probe whether this process can actually read the Biome remote dir.

    ``.exists()`` alone is not enough: without Full Disk Access macOS lets the path
    resolve but raises ``PermissionError`` on the first real read, so we force a read
    by pulling one entry from the directory iterator.
    """
    try:
        if not BIOME_REMOTE.exists():
            return False, f"{BIOME_REMOTE} does not exist"
        next(iter(BIOME_REMOTE.iterdir()), None)  # forces a TCC-gated read
        return True, ""
    except PermissionError as e:
        return False, str(e)


def import_once(con) -> dict[str, int]:
    """Import all synced devices once. Returns {guid: new_rows_inserted}."""
    result: dict[str, int] = {}
    if not BIOME_REMOTE.exists():
        return result
    for guid_dir in sorted(BIOME_REMOTE.iterdir()):
        if not guid_dir.is_dir():
            continue
        events = parse_device_dir(guid_dir)
        if not events:
            continue
        kind = classify_device([e.bundle_id for e in events if e.bundle_id])
        rows = [(guid_dir.name, kind, e.bundle_id, int(e.ts), e.focus) for e in events]
        result[guid_dir.name] = insert_device_events(con, rows)
    return result


def run(interval_seconds: int = 600) -> None:
    """Import on a loop (for the background collector).

    Never lets one failed pass kill the loop: a PermissionError means this process lacks
    Full Disk Access to ~/Library/Biome (common when launched by launchd). We log a clear
    hint and retry — so once Full Disk Access is granted, importing starts working with no
    reinstall needed.
    """
    con = connect()
    warned = False
    while True:
        try:
            added = import_once(con)
            warned = False
            if added:
                stamp = time.strftime("%H:%M:%S")
                for guid, n in added.items():
                    if n:
                        print(f"[{stamp}] imported {n} new events from {guid[:8]}", flush=True)
        except PermissionError:
            if not warned:  # avoid spamming the log every interval
                print(
                    "[importer] No access to ~/Library/Biome — grant Full Disk Access to this "
                    "python3 in System Settings → Privacy & Security → Full Disk Access. "
                    "Retrying; no reinstall needed.",
                    flush=True,
                )
                warned = True
        except Exception as e:  # keep the collector alive on any unexpected error
            print(f"[importer] error: {e!r} (retrying)", flush=True)
        time.sleep(interval_seconds)


if __name__ == "__main__":
    con = connect()
    added = import_once(con)
    total = con.execute("SELECT COUNT(*) FROM device_events").fetchone()[0]
    print("new events:", added)
    print("total device_events in DB:", total)
