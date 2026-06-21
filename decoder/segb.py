#!/usr/bin/env python3
"""SEGB decoder for Apple Biome ``App.InFocus`` streams.

This is the riskiest, reverse-engineered part of the tracker (see FINDINGS.md / TODO Phase 1).
It reads Apple's binary SEGB segment files and returns app-focus events for synced devices
(iPhone, Apple Watch).

Verified format of an ``App.InFocus`` SEGB segment (512 KiB fixed file):

  * Bytes 0..3   : magic ``b"SEGB"``.
  * Bytes 4..7   : record count (uint32 LE).
  * Bytes 8..15  : segment creation time (CFAbsoluteTime double).
  * Bytes 16..31 : reserved header fields.
  * From byte 32 : record data region — protobuf payloads laid out back-to-back, each
                   record preceded by a short, variable-length alignment sub-header.
  * From EOF up  : a metadata table of 16-byte entries growing *backwards* from the end of
                   the file. Reading from EOF gives records in chronological order. Each
                   entry is ``[end_offset uint32][state uint32][timestamp double]`` where
                   ``end_offset`` is the cumulative end of that record relative to byte 32,
                   ``state`` is 1 for a live record, and ``timestamp`` is the event time
                   (CFAbsoluteTime — it matches protobuf field 4 of the record).

Each record's protobuf (App.InFocus) carries:
  * field 2 (varint) — a small flag (observed always 1),
  * field 3 (varint) — the **focus flag**: ``1`` = the app *gained* focus (session start),
    ``0`` = the app *lost* focus (session end). Pairing a start with the next matching end
    for the same app gives the exact foreground duration (this matches iOS Screen Time),
  * field 4 (double) — event time (same as the metadata timestamp),
  * field 6 (string) — the app **bundle id**,
  * fields 9/10 (strings) — app version / build,
  * field 13 (varint) — a flag.

We take the event time from the metadata table (always present and reliable) and the
bundle id + focus flag from the protobuf. Timestamps are CFAbsoluteTime; add ``MAC_EPOCH``
for Unix.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

# CFAbsoluteTime epoch (2001-01-01 UTC) offset to Unix epoch.
MAC_EPOCH = 978307200

_MAGIC = b"SEGB"
_HEADER_LEN = 32
_META_ENTRY = struct.Struct("<IId")  # end_offset, state, timestamp(double)
# An App.InFocus protobuf reliably starts with field 2 (tag 0x10, small varint) followed
# by field 3 (tag 0x18). We search for this to skip the variable alignment sub-header.
_PB_START = re.compile(rb"\x10[\x00-\x03]\x18")
# Plausible CFAbsoluteTime window so we can reject garbage metadata entries:
# ~2014 (4e8) .. ~2033 (1e9). Real events fall comfortably inside.
_TS_MIN, _TS_MAX = 4e8, 1e9


@dataclass(frozen=True)
class FocusEvent:
    """One ``App.InFocus`` record for ``bundle_id`` at ``ts`` (Unix seconds).

    ``focus`` is 1 when the app gained focus (session start), 0 when it lost focus
    (session end), or None if the flag couldn't be read. Pair a start with the next
    matching end for the same app to get the exact foreground duration.
    """

    ts: float          # Unix epoch seconds (UTC)
    bundle_id: str | None
    focus: int | None = None


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    """Decode a base-128 varint at ``buf[i]``; return (value, next_index)."""
    value = shift = 0
    while True:
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7


def _parse_record(record: bytes) -> tuple[str | None, int | None]:
    """Walk a record's protobuf; return (bundle_id from field 6, focus flag from field 3)."""
    m = _PB_START.search(record)
    if not m:
        return None, None
    bundle_id: str | None = None
    focus: int | None = None
    i, n = m.start(), len(record)
    while i < n:
        try:
            tag, i = _read_varint(record, i)
            field, wire = tag >> 3, tag & 7
            if wire == 0:        # varint
                value, i = _read_varint(record, i)
                if field == 3:
                    focus = value
            elif wire == 1:      # 64-bit
                i += 8
            elif wire == 2:      # length-delimited
                length, i = _read_varint(record, i)
                if field == 6:
                    try:
                        bundle_id = record[i : i + length].decode()
                    except UnicodeDecodeError:
                        bundle_id = None
                i += length
            elif wire == 5:      # 32-bit
                i += 4
            else:
                break
        except IndexError:
            break
    return bundle_id, focus


def parse_segb_file(path: str | Path) -> list[FocusEvent]:
    """Decode one ``App.InFocus`` SEGB segment into chronological focus events.

    Returns an empty list for non-SEGB or unreadable files (e.g. the 0-byte ``lock``).
    """
    data = Path(path).read_bytes()
    if len(data) < _HEADER_LEN or data[:4] != _MAGIC:
        return []

    # Read the metadata table backwards from EOF. Each entry's end_offset is cumulative;
    # consecutive offsets delimit the record bytes in the data region (which starts at 32).
    entries: list[tuple[int, float]] = []  # (end_offset, ts_unix)
    off = len(data) - _META_ENTRY.size
    while off >= _HEADER_LEN:
        end_off, state, ts = _META_ENTRY.unpack_from(data, off)
        if not (end_off and end_off < len(data) and state in (0, 1) and _TS_MIN < ts < _TS_MAX):
            break
        if state == 1:  # 1 = live record; skip tombstoned/deleted entries
            entries.append((end_off, ts + MAC_EPOCH))
        off -= _META_ENTRY.size

    events: list[FocusEvent] = []
    prev_end = 0
    # entries are newest-table-row-first == chronological; walk in that order so prev_end
    # tracks the cumulative record boundary correctly.
    for end_off, ts in entries:
        record = data[_HEADER_LEN + prev_end : _HEADER_LEN + end_off]
        prev_end = end_off
        bundle_id, focus = _parse_record(record)
        events.append(FocusEvent(ts=ts, bundle_id=bundle_id, focus=focus))
    return events


def parse_device_dir(device_dir: str | Path) -> list[FocusEvent]:
    """Decode every SEGB segment under one ``remote/<GUID>/`` device directory.

    Skips the ``tombstone/`` subdir and the ``lock`` file. De-duplicates events that
    appear in overlapping segments (same timestamp + bundle), and returns them sorted.
    """
    device_dir = Path(device_dir)
    seen: set[tuple[float, str | None, int | None]] = set()
    out: list[FocusEvent] = []
    for f in device_dir.iterdir():
        if f.is_dir() or f.name == "lock":
            continue
        for ev in parse_segb_file(f):
            key = (ev.ts, ev.bundle_id, ev.focus)
            if key not in seen:
                seen.add(key)
                out.append(ev)
    out.sort(key=lambda e: e.ts)
    return out


if __name__ == "__main__":
    import sys

    for p in sys.argv[1:]:
        evs = parse_segb_file(p)
        print(f"{p}: {len(evs)} events")
        for e in evs[:5]:
            print(f"  {e.ts:.0f}  {e.bundle_id}")
