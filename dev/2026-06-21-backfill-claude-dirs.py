#!/usr/bin/env python3
"""One-time backfill: relabel Warp "(other)" samples with the Claude Code directory.

While a Claude Code session runs in Warp, the terminal title shows a task name / spinner,
never the cwd — so those samples were stored with a non-path title and bucket as "(other)".
Each session logs to ~/.claude/projects/<slug>/<id>.jsonl with the literal ``cwd`` and a
per-message ``timestamp``. This script reconstructs, per session, the wall-clock intervals it
was active, then rewrites every Warp "(other)" sample that falls inside an interval so its
``window_title`` becomes the session's home-relative cwd (e.g. ``~/CDBS_Control``). The
dashboard's query-side ``project_root()`` then resolves it like a normal prompt cwd.

Only the directory matters; session id/name are ignored. Samples covered by no session stay
"(other)" (genuine non-Claude foreground programs). Dry-run by default; pass --apply to write.

Usage:
    python3 dev/2026-06-21-backfill-claude-dirs.py            # report only
    python3 dev/2026-06-21-backfill-claude-dirs.py --apply    # write changes
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collector"))
from appinfo import _abs_to_home, _first_cwd, project_root  # noqa: E402

WARP_BUNDLE = "dev.warp.Warp-Stable"
DB = os.path.join(os.path.dirname(__file__), "..", "data", "timetracker.sqlite")
PROJECTS = os.path.expanduser("~/.claude/projects")
GAP = 300.0   # message gaps longer than this split one active interval into two
PAD = 60.0    # widen each interval by this so a sample just after a turn still attributes


def _epoch(iso: str) -> float | None:
    """'2026-06-21T19:58:28.538Z' → Unix epoch (UTC-correct), or None if unparseable."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def session_intervals() -> list[tuple[float, float, str, list[float]]]:
    """(start, end, rel_cwd, sorted_timestamps) for every active stretch of every session."""
    out: list[tuple[float, float, str, list[float]]] = []
    for path in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        cwd = _first_cwd(path)
        rel = _abs_to_home(cwd) if cwd else None
        if not rel:
            continue
        ts: list[float] = []
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    t = json.loads(line).get("timestamp")
                except json.JSONDecodeError:
                    continue
                e = _epoch(t) if t else None
                if e is not None:
                    ts.append(e)
        if not ts:
            continue
        ts.sort()
        start = prev = ts[0]
        group = [ts[0]]
        for e in ts[1:]:
            if e - prev > GAP:
                out.append((start - PAD, prev + PAD, rel, group))
                start, group = e, []
            group.append(e)
            prev = e
        out.append((start - PAD, prev + PAD, rel, group))
    return out


def _nearest(sorted_ts: list[float], ts: float) -> float:
    """Smallest |t - ts| over a sorted timestamp list (bisect, O(log n))."""
    import bisect
    i = bisect.bisect_left(sorted_ts, ts)
    best = float("inf")
    for j in (i - 1, i):
        if 0 <= j < len(sorted_ts):
            best = min(best, abs(sorted_ts[j] - ts))
    return best


def resolve(intervals, ts: float) -> str | None:
    """Home-relative cwd for a sample at ``ts``: among sessions whose active interval covers
    ts, the one with a message closest to ts wins (handles overlapping sessions / tabs)."""
    best_rel, best_d = None, float("inf")
    for start, end, rel, group in intervals:
        if start <= ts <= end:
            d = _nearest(group, ts)
            if d < best_d:
                best_rel, best_d = rel, d
    return best_rel


def main() -> None:
    apply = "--apply" in sys.argv[1:]
    intervals = session_intervals()
    print(f"loaded {len(intervals)} active session intervals")

    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT rowid, ts, window_title FROM mac_samples WHERE bundle_id = ?",
        (WARP_BUNDLE,),
    ).fetchall()

    updates: list[tuple[str, int]] = []
    by_dir: Counter = Counter()
    for rowid, ts, title in rows:
        if project_root(title) is not None:
            continue  # already has a real cwd (idle prompt) — leave it
        rel = resolve(intervals, float(ts))
        if rel:
            updates.append((rel, rowid))
            by_dir[project_root(rel)] += 1

    total_other = sum(1 for _, _, t in rows if project_root(t) is None)
    print(f"Warp samples total: {len(rows)}   currently '(other)': {total_other}")
    print(f"would relabel: {len(updates)}   (stay '(other)': {total_other - len(updates)})")
    for d, n in by_dir.most_common():
        print(f"  {n:6d} samples -> {d}")

    if not apply:
        print("\nDRY RUN — re-run with --apply to write these changes.")
        return
    con.executemany("UPDATE mac_samples SET window_title = ? WHERE rowid = ?", updates)
    con.commit()
    print(f"\nAPPLIED: {len(updates)} rows updated.")


if __name__ == "__main__":
    main()
