#!/usr/bin/env python3
"""SQLite storage for the time tracker — schema + write helpers.

One local database (``data/timetracker.sqlite``). Two sources that never overlap, so Mac
usage is never double-counted:

  * ``mac_samples``  — live foreground samples from this Mac (one row per poll). Each row
    represents ``interval`` seconds of the named app being frontmost (unless ``idle``).
  * ``device_events`` — app-focus events imported from Apple Biome for *synced* devices
    (iPhone, Apple Watch) only. We import the Biome ``remote/`` streams, never the Mac's
    own ``local/`` stream, so these are strictly phone/watch usage. Durations are derived
    at query time (an app is in focus from its event until the next event on that device).

All timestamps are Unix epoch seconds (UTC), stored as INTEGER.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "timetracker.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mac_samples (
    id            INTEGER PRIMARY KEY,
    ts            INTEGER NOT NULL,        -- Unix epoch seconds (sample start)
    interval      INTEGER NOT NULL,        -- seconds this sample represents
    bundle_id     TEXT,
    app_name      TEXT,
    window_title  TEXT,
    url           TEXT,                    -- Comet active-tab URL, if app is Comet
    domain        TEXT,                    -- host extracted from url
    idle          INTEGER NOT NULL DEFAULT 0  -- 1 if user was idle during this sample
);
CREATE INDEX IF NOT EXISTS idx_mac_ts   ON mac_samples(ts);
CREATE INDEX IF NOT EXISTS idx_mac_app  ON mac_samples(bundle_id);

CREATE TABLE IF NOT EXISTS device_events (
    id         INTEGER PRIMARY KEY,
    device     TEXT NOT NULL,              -- remote GUID
    kind       TEXT NOT NULL,              -- 'iphone' | 'watch' | 'unknown'
    bundle_id  TEXT,
    ts         INTEGER NOT NULL,           -- Unix epoch seconds (focus event time)
    focus      INTEGER,                    -- 1 = gained focus (start), 0 = lost focus (end)
    UNIQUE(device, ts, bundle_id, focus)   -- dedup re-imports of overlapping segments
);
CREATE INDEX IF NOT EXISTS idx_dev_ts   ON device_events(ts);
CREATE INDEX IF NOT EXISTS idx_dev_app  ON device_events(bundle_id);
"""


def connect(path: str | Path = DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the database and ensure the schema exists."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")  # let the dashboard read while the collector writes
    con.executescript(_SCHEMA)
    return con


def connect_ro(path: str | Path = DB_PATH) -> sqlite3.Connection:
    """Open the database read-only (for the dashboard)."""
    from urllib.request import pathname2url

    uri = "file:" + pathname2url(str(Path(path))) + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def insert_mac_sample(
    con: sqlite3.Connection,
    ts: int,
    interval: int,
    bundle_id: str | None,
    app_name: str | None,
    window_title: str | None,
    url: str | None,
    domain: str | None,
    idle: bool,
) -> None:
    con.execute(
        "INSERT INTO mac_samples (ts, interval, bundle_id, app_name, window_title, url, domain, idle)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (ts, interval, bundle_id, app_name, window_title, url, domain, int(idle)),
    )
    con.commit()


def insert_device_events(
    con: sqlite3.Connection, rows: list[tuple[str, str, str | None, int, int | None]]
) -> int:
    """Bulk-insert (device, kind, bundle_id, ts, focus) rows; duplicates are ignored.

    Returns the number of new rows actually inserted.
    """
    before = con.total_changes
    con.executemany(
        "INSERT OR IGNORE INTO device_events (device, kind, bundle_id, ts, focus) VALUES (?,?,?,?,?)",
        rows,
    )
    con.commit()
    return con.total_changes - before
