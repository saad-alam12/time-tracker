#!/usr/bin/env python3
"""Build the dashboard's day summary from the database.

Unifies two sources into one view of a single calendar day (local time):
  * Mac live samples — each non-idle row counts ``interval`` seconds for its app; rows with
    a browser URL also count toward that website's domain.
  * Device focus events (iPhone/Watch) — an app is in focus from its event until the next
    event on that device; gaps longer than ``device_gap_cap_seconds`` are treated as the
    screen being off and not counted.

Classification is **tag-first**: every chunk carries a set of tags (see ``categorize.Tagger``).
From the tags we derive, at query time:
  * **role totals** (Focus / Productive / Fun) for the hero rings — each tag has a role;
  * **emergent category totals** — a category is a named union of tags, so categories may
    overlap and their totals can exceed the day total;
  * per-tag totals and per-app/site/page tag lists.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

from appinfo import canonical_url, friendly_name, obsidian_note, project_root  # noqa: E402
from categorize import (  # noqa: E402
    Tagger, ignored_sets, load_ignored, load_category_defs, load_timeline_prefs,
)

_SETTINGS = Path(__file__).resolve().parent.parent / "config" / "settings.json"

WARP_BUNDLE = "dev.warp.Warp-Stable"  # only app with a per-directory drill-down
OBSIDIAN_BUNDLE = "md.obsidian"       # per-note drill-down (window title holds the note name)

# Work hours window (local) used for the "fun during work" metric.
WORK_START, WORK_END = 9, 18
# Apps whose opens are counted as distractions.
DISTRACTION = {"com.zhiliaoapp.musically", "com.burbn.instagram"}  # TikTok, Instagram
DEEP_MIN = 1500   # a "deep-work block" is ≥25 min of uninterrupted focus
DEEP_GAP = 120    # bridge focus gaps up to this many seconds (matches idle threshold)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    """Seconds the interval [a0,a1) overlaps [b0,b1)."""
    return max(0.0, min(a1, b1) - max(a0, b0))


def _max_session() -> int:
    """Drop pathological sessions (a start whose matching end was lost) longer than this."""
    return int(json.loads(_SETTINGS.read_text()).get("max_session_seconds", 14400))


def _settings() -> dict:
    return json.loads(_SETTINGS.read_text())


def _goals() -> dict:
    """Daily targets for the Activity rings (editable in settings.json)."""
    g = _settings().get("goals", {})
    return {
        "focus_seconds": int(g.get("focus_seconds", 14400)),        # 4h
        "productive_seconds": int(g.get("productive_seconds", 7200)),  # 2h
        "fun_cap_seconds": int(g.get("fun_cap_seconds", 7200)),     # 2h cap
        "deep_blocks": int(g.get("deep_blocks", 3)),
    }


def tag_roles() -> dict:
    """Which tags count as Focus / Productive / Fun (drives the hero rings). A tag in none of
    the three lists is 'Neither' and counts toward no ring. Editable from the Roles panel."""
    r = _settings().get("tag_roles")
    if not isinstance(r, dict):
        return {"focus": [], "productive": [], "fun": []}
    return {k: [str(x) for x in (r[k] if isinstance(r.get(k), list) else [])]
            for k in ("focus", "productive", "fun")}


def save_tag_roles(focus: list[str], productive: list[str], fun: list[str]) -> dict:
    """Persist tag roles into settings.json, preserving other keys. A tag has one role only —
    duplicates are dropped with precedence Focus > Productive > Fun."""
    focus = list(dict.fromkeys(focus))
    productive = [t for t in dict.fromkeys(productive) if t not in focus]
    fun = [t for t in dict.fromkeys(fun) if t not in focus and t not in productive]
    cfg = _settings()
    cfg["tag_roles"] = {"focus": focus, "productive": productive, "fun": fun}
    _SETTINGS.write_text(json.dumps(cfg, indent=2) + "\n")
    return cfg["tag_roles"]


def _role_of(roles: dict) -> dict[str, str]:
    """Map each tag to its role. Insert Fun, then Productive, then Focus so Focus wins ties."""
    m: dict[str, str] = {}
    for r in ("fun", "productive", "focus"):
        for t in roles.get(r, []):
            m[t] = r
    return m


def _baseline_days() -> int:
    return int(_settings().get("baseline_days", 14))


def _fmt(seconds: float) -> str:
    """'5h 2m' / '47m' — server-side duration formatter for plain-language insights."""
    m = round(seconds / 60)
    if m < 60:
        return f"{m}m"
    return f"{m // 60}h {m % 60}m"


def _bucket_hours(hourly: list, s: float, e: float, day_start: int) -> None:
    """Distribute the interval [s,e) into 24 per-hour bins (clipped to the day)."""
    s = max(s, day_start)
    e = min(e, day_start + 86400)
    while s < e:
        h = int((s - day_start) // 3600)
        if not 0 <= h < 24:
            break
        h_end = day_start + (h + 1) * 3600
        chunk = min(e, h_end)
        hourly[h] += chunk - s
        s = chunk


def day_bounds(date_str: str) -> tuple[int, int]:
    """Unix [start, end) for a local-time calendar day 'YYYY-MM-DD'."""
    d = dt.date.fromisoformat(date_str)
    start = dt.datetime(d.year, d.month, d.day)  # local midnight
    return int(start.timestamp()), int((start + dt.timedelta(days=1)).timestamp())


def available_dates(con) -> list[str]:
    """Distinct local dates that have any data, newest first."""
    seen = set()
    for (ts,) in con.execute("SELECT ts FROM mac_samples UNION SELECT ts FROM device_events"):
        seen.add(dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d"))
    return sorted(seen, reverse=True)


def ignored_list() -> list[dict]:
    """Hidden apps/sites with display names, for the dashboard's 'Hidden' panel."""
    out = []
    for it in load_ignored().get("ignored", []):
        key, typ = it.get("key"), it.get("type")
        if not key or typ not in ("app", "site"):
            continue
        out.append({"type": typ, "key": key,
                    "name": friendly_name(key) if typ == "app" else key})
    return out


def _add(acc: dict, key, seconds: float) -> None:
    acc[key] = acc.get(key, 0.0) + seconds


def _better_page(a: str, b: str) -> bool:
    """Is page key ``a`` more specific than ``b``? Prefer one with a query (a real
    ``watch?v=…`` URL) over a bare host, then the longer key."""
    if ("?" in a) != ("?" in b):
        return "?" in a
    return len(a) > len(b)


def build_summary(con, date_str: str, include_prev: bool = True) -> dict:
    start, end = day_bounds(date_str)
    tgr = Tagger()
    ign_apps, ign_sites = ignored_sets()  # hidden apps/sites: dropped from every total below

    roles = tag_roles()
    role_of = _role_of(roles)
    cat_defs = [{"name": c["name"], "tags": c["tags"], "_tagset": set(c["tags"])}
                for c in load_category_defs()]

    apps: dict[tuple, float] = {}      # (bundle_id, name, source) -> seconds
    websites: dict[str, float] = {}    # domain -> seconds
    web_meta: dict[str, list] = {}     # domain -> [sessions, first_ts, last_ts]
    subpages: dict[str, dict[str, list]] = {}  # domain -> canonical-url -> [seconds, label]
    directories: dict[str, list] = {}  # Warp project root -> [seconds, sessions, first_ts, last_ts]
    notes: dict[str, list] = {}        # Obsidian note title -> [seconds, sessions, first_ts, last_ts]
    tag_totals: dict[str, float] = {}      # tag -> seconds
    role_totals: dict[str, float] = {"focus": 0.0, "productive": 0.0, "fun": 0.0}
    cat_totals: dict[str, float] = {}      # emergent category name -> seconds (overlapping)
    segments: list[tuple] = []         # (start, end, bundle_id, name, source, title, tags)
    total_secs = 0.0                   # all non-idle tracked time (the day total)

    def _absorb(tags: list[str], secs: float) -> None:
        """Fold one chunk's tags into the per-tag, per-role and per-category totals."""
        present_roles: set[str] = set()
        for t in tags:
            tag_totals[t] = tag_totals.get(t, 0.0) + secs
            r = role_of.get(t)
            if r:
                present_roles.add(r)
        for r in present_roles:           # a chunk counts once per distinct role present
            role_totals[r] += secs
        if cat_defs:
            tset = set(tags)
            for cd in cat_defs:
                if tset & cd["_tagset"]:   # overlapping membership — categories may double-count
                    cat_totals[cd["name"]] = cat_totals.get(cd["name"], 0.0) + secs

    # --- Mac live samples ---
    # Ordered by ts so consecutive samples of the same app merge into one focus session.
    rows = con.execute(
        "SELECT ts, interval, bundle_id, app_name, url, domain, window_title FROM mac_samples"
        " WHERE ts >= ? AND ts < ? AND idle = 0 ORDER BY ts",
        (start, end),
    ).fetchall()
    cur: list | None = None      # open app session [start, end, bundle, name, source, title, tags]
    cur_dom: str | None = None   # domain of the currently open browser session (for site sessions)
    cur_dir: str | None = None   # Warp project root of the current sample (for dir sessions)
    cur_note: str | None = None  # Obsidian note of the current sample (for note sessions)
    for ts, interval, bundle_id, app_name, url, domain, window_title in rows:
        # Skip hidden apps, and hidden sites (a browser sample tagged with an ignored domain
        # is dropped whole — that time belonged to the hidden site).
        if bundle_id in ign_apps or (domain and domain.lower() in ign_sites):
            continue
        secs = float(interval)
        total_secs += secs
        name = friendly_name(bundle_id, app_name)
        _add(apps, (bundle_id, name, "mac"), secs)
        page = canonical_url(url) if url else None
        # For Warp, the window title holds the cwd; resolve its project root so the sample's
        # tags can follow a per-directory tag (dir_key is None for every other app).
        dk = project_root(window_title) if bundle_id == WARP_BUNDLE else None
        # For Obsidian, the window title holds the open note's name; resolve it so the sample's
        # tags can follow a per-note tag (note_key is None for every other app).
        note_key = obsidian_note(window_title) if bundle_id == OBSIDIAN_BUNDLE else None
        sample_tags = sorted(tgr.tags_for(bundle_id, domain, page, dk, note_key, ts))
        _absorb(sample_tags, secs)
        if domain:
            _add(websites, domain, secs)
            m = web_meta.get(domain)
            # Count a new site session when the previous sample wasn't this same domain.
            if m is None:
                web_meta[domain] = [1, ts, ts + interval]
            else:
                if cur_dom != domain:
                    m[0] += 1
                m[2] = ts + interval
            if page:
                sp = subpages.setdefault(domain, {})
                e = sp.get(page)
                if e is None:
                    sp[page] = [secs, window_title or None]
                else:
                    e[0] += secs
                    if window_title and not e[1]:  # keep the first non-empty page title seen
                        e[1] = window_title
        # Per-directory breakdown for Warp (parallels the per-site websites block above).
        # Titles that aren't paths (a running program owned the title) bucket as "(other)".
        if bundle_id == WARP_BUNDLE:
            dkey = dk or "(other)"
            de = directories.get(dkey)
            if de is None:
                directories[dkey] = [secs, 1, ts, ts + interval]
            else:
                de[0] += secs
                if cur_dir != dkey:  # re-entering a dir after leaving it counts a new session
                    de[1] += 1
                de[2] = min(de[2], ts)
                de[3] = ts + interval
            cur_dir = dkey
        else:
            cur_dir = None
        # Per-note breakdown for Obsidian (parallels the Warp directory block above). Samples
        # with no resolvable note title (none open / non-Obsidian title) bucket as "(other)".
        if bundle_id == OBSIDIAN_BUNDLE:
            nkey = note_key or "(other)"
            ne = notes.get(nkey)
            if ne is None:
                notes[nkey] = [secs, 1, ts, ts + interval]
            else:
                ne[0] += secs
                if cur_note != nkey:  # re-opening a note after leaving it counts a new session
                    ne[1] += 1
                ne[2] = min(ne[2], ts)
                ne[3] = ts + interval
            cur_note = nkey
        else:
            cur_note = None
        # Merge contiguous same-app samples (a gap > one interval ends the session).
        if cur and cur[2] == bundle_id and ts - cur[1] <= interval:
            cur[1] = ts + secs
            if window_title:
                cur[5] = window_title  # keep the latest tab/window title seen in the session
            if sample_tags:           # union tags across the session (e.g. Warp dir tags mid-run)
                cur[6] = sorted(set(cur[6]) | set(sample_tags))
        else:
            if cur:
                segments.append(tuple(cur))
            cur = [ts, ts + secs, bundle_id, name, "mac", window_title or None, sample_tags]
        cur_dom = domain
    if cur:
        segments.append(tuple(cur))

    # --- Device focus sessions (iPhone / Watch) ---
    # App.InFocus records are start (focus=1) / end (focus=0) markers. We pair each start
    # with the next matching end for the same app to get the exact foreground session, then
    # clip it to the day. This matches iOS Screen Time (no gap-filling guesswork).
    max_session = _max_session()
    for device, kind in con.execute("SELECT DISTINCT device, kind FROM device_events"):
        # Fetch a margin around the day so sessions crossing midnight are still paired.
        evs = con.execute(
            "SELECT ts, bundle_id, focus FROM device_events"
            " WHERE device = ? AND ts >= ? AND ts < ? ORDER BY ts, focus DESC",
            (device, start - max_session, end + max_session),
        ).fetchall()
        open_start: dict[str | None, int] = {}  # bundle_id -> session start ts
        for ts, bundle_id, focus in evs:
            if bundle_id in ign_apps:  # hidden app — drop its device focus sessions too
                continue
            if focus == 1:
                open_start[bundle_id] = ts
            elif focus == 0 and bundle_id in open_start:
                s0 = open_start.pop(bundle_id)
                if not (0 < ts - s0 <= max_session):
                    continue
                seg_start, seg_end = max(s0, start), min(ts, end)
                if seg_end <= seg_start:
                    continue
                secs = float(seg_end - seg_start)
                total_secs += secs
                nm = friendly_name(bundle_id)
                _add(apps, (bundle_id, nm, kind), secs)
                dev_tags = sorted(tgr.tags_for(bundle_id=bundle_id, ts=seg_start))
                _absorb(dev_tags, secs)
                # no window title on device apps
                segments.append((seg_start, seg_end, bundle_id, nm, kind, None, dev_tags))

    # --- Shape the response ---
    # Warp's per-directory breakdown: the project roots worked in that day, each with its
    # current tags. The "(other)" bucket holds time whose title wasn't a path.
    dir_list = [
        {"dir": dk, "seconds": round(sec), "sessions": sess_n,
         "tags": [] if dk == "(other)" else sorted(tgr.tags_for(dir_key=dk, ts=end))}
        for dk, (sec, sess_n, _first, _last) in sorted(directories.items(), key=lambda x: -x[1][0])
        if sec >= 60
    ]
    # Obsidian's per-note breakdown: the notes opened that day, each with its current tags.
    # The "(other)" bucket holds time when no note title could be resolved.
    note_list = [
        {"note": nk, "seconds": round(sec), "sessions": sess_n,
         "tags": [] if nk == "(other)" else sorted(tgr.tags_for(note=nk, ts=end))}
        for nk, (sec, sess_n, _first, _last) in sorted(notes.items(), key=lambda x: -x[1][0])
        if sec >= 60
    ]
    app_list = [
        {"bundle_id": b, "name": n, "source": s, "seconds": round(sec),
         "tags": sorted(tgr.tags_for(bundle_id=b, ts=end)),
         **({"directories": dir_list} if b == WARP_BUNDLE else {}),
         **({"notes": note_list} if b == OBSIDIAN_BUNDLE else {})}
        for (b, n, s), sec in sorted(apps.items(), key=lambda x: -x[1])
    ]
    # Drop fleeting visits: only show sites used for at least 2 minutes. The browsing split
    # (productive / distracting / neutral) is driven by each site's tag roles.
    web_list = []
    browsing = {"productive": 0.0, "distracting": 0.0, "neutral": 0.0}
    for d, sec in sorted(websites.items(), key=lambda x: -x[1]):
        wtags = tgr.tags_for(domain=d, ts=end)
        wroles = {role_of.get(t) for t in wtags} - {None}
        bucket = ("productive" if wroles & {"focus", "productive"}
                  else "distracting" if "fun" in wroles else "neutral")
        browsing[bucket] += sec
        if sec >= 120:
            meta = web_meta.get(d, [0, 0, 0])
            # Subpages: individual pages/videos on this domain with ≥3 min that day. Merge
            # pages sharing the same title first (YouTube's active-tab URL sometimes lags as
            # the bare host while the title is already the video). Keep the most specific key.
            merged: dict[str, list] = {}  # group key -> [seconds, label, page]
            for pg, (psec, label) in subpages.get(d, {}).items():
                gk = f"t:{label}" if label else f"p:{pg}"
                e = merged.get(gk)
                if e is None:
                    merged[gk] = [psec, label, pg]
                else:
                    e[0] += psec
                    if _better_page(pg, e[2]):
                        e[2] = pg
            subs = [
                {"page": e[2], "label": e[1] or e[2], "seconds": round(e[0]),
                 # A subpage inherits its site's tags: pass `domain` too so a single
                 # site-level tag cascades to every subpage (now and on future visits)
                 # without hand-tagging each page. Explicit page tags still add on top.
                 "tags": sorted(tgr.tags_for(domain=d, page=e[2], ts=end))}
                for e in sorted(merged.values(), key=lambda x: -x[0])
                if e[0] >= 180
            ]
            web_list.append({
                "domain": d, "seconds": round(sec),
                "sessions": meta[0], "first": meta[1], "last": meta[2], "subpages": subs,
                "tags": sorted(tgr.tags_for(domain=d, ts=end)),
            })
    browsing = {k: round(v) for k, v in browsing.items()}

    cat_list = [{"name": nm, "seconds": round(sec)}
                for nm, sec in sorted(cat_totals.items(), key=lambda x: -x[1])]

    sessions = [
        {"start": int(max(s, start)), "end": int(min(e, end)),
         "bundle_id": b, "name": n, "source": src, "title": t, "tags": tg}
        for (s, e, b, n, src, t, tg) in sorted(segments, key=lambda r: (r[0], r[1]))
        if min(e, end) > max(s, start)
    ]

    goals = _goals()
    stats = _compute_stats(segments, role_totals, set(roles["focus"]), set(roles["fun"]),
                           start, total_secs)
    if include_prev:
        prev_date = (dt.date.fromisoformat(date_str) - dt.timedelta(days=1)).isoformat()
        p = build_summary(con, prev_date, include_prev=False)["stats"]
        stats["prev"] = {"total": p["total"], "focus": p["focus"],
                         "productive": p["productive"], "fun": p["fun"]}
        stats["focus_streak"] = _focus_streak(con, date_str, goals["focus_seconds"])

    return {
        "date": date_str,
        "total_seconds": round(total_secs),
        "day_start": start,
        "apps": app_list,
        "websites": web_list,
        "browsing": browsing,
        "categories": cat_list,                       # emergent categories, ranked
        "category_defs": [{"name": c["name"], "tags": c["tags"]} for c in cat_defs],
        "tags": [{"tag": t, "seconds": round(sec)}
                 for t, sec in sorted(tag_totals.items(), key=lambda x: -x[1])],
        "tag_roles": roles,
        "sessions": sessions,
        "timeline_prefs": {"tag": load_timeline_prefs("tag"),
                           "category": load_timeline_prefs("category")},
        "stats": stats,
        "goals": goals,
        "insights": build_insights(con, date_str) if include_prev else [],
    }


def _compute_stats(segments: list[tuple], role_totals: dict, focus_tags: set[str],
                   fun_tags: set[str], day_start: int, total_secs: float) -> dict:
    """Derive the stat-card metrics from the day's role totals and focus sessions."""
    focus = role_totals.get("focus", 0.0)
    productive = role_totals.get("productive", 0.0)
    fun = role_totals.get("fun", 0.0)

    # Deep-work: merge Focus-role sessions (bridging short gaps) into uninterrupted blocks.
    prod = sorted((s, e) for (s, e, _b, _n, _src, _t, tags) in segments
                  if any(t in focus_tags for t in tags))
    blocks: list[list[float]] = []
    for s, e in prod:
        if blocks and s - blocks[-1][1] <= DEEP_GAP:
            blocks[-1][1] = max(blocks[-1][1], e)
        else:
            blocks.append([s, e])
    deep = [b for b in blocks if b[1] - b[0] >= DEEP_MIN]
    longest = max(blocks, key=lambda b: b[1] - b[0], default=None)

    # Fun time during work hours (local 9–18h) + number of TikTok/Instagram opens.
    ws, we = day_start + WORK_START * 3600, day_start + WORK_END * 3600
    fun_work = sum(_overlap(s, e, ws, we) for (s, e, _b, _n, _src, _t, tags) in segments
                   if any(t in fun_tags for t in tags))
    distraction_opens = sum(1 for (_s, _e, b, _n, _src, _t, _tags) in segments if b in DISTRACTION)

    return {
        "total": round(total_secs),
        "focus": round(focus),
        "productive": round(productive),
        "fun": round(fun),
        "deep_block_count": len(deep),
        "deep_longest": round(longest[1] - longest[0]) if longest else 0,
        "deep_longest_start": int(longest[0]) if longest else None,
        "fun_work": round(fun_work),
        "distraction_opens": distraction_opens,
    }


def build_trend(con, end_date: str, days: int = 7) -> dict:
    """Per-emergent-category totals for the last ``days`` days ending on ``end_date``."""
    d = dt.date.fromisoformat(end_date)
    out, totals = [], {}
    for i in range(days - 1, -1, -1):
        day = (d - dt.timedelta(days=i)).isoformat()
        s = build_summary(con, day, include_prev=False)
        catmap = {c["name"]: c["seconds"] for c in s["categories"]}
        for k, v in catmap.items():
            totals[k] = totals.get(k, 0) + v
        out.append({"date": day, "total": s["total_seconds"], "categories": catmap})
    order = [k for k, _ in sorted(totals.items(), key=lambda x: -x[1])]
    return {"days": out, "categories": order}


def _focus_streak(con, date_str: str, focus_goal: int, cap: int = 60) -> int:
    """Consecutive days ending on date_str whose focus time met the goal (0 if today missed)."""
    d = dt.date.fromisoformat(date_str)
    streak = 0
    for i in range(cap):
        day = (d - dt.timedelta(days=i)).isoformat()
        f = build_summary(con, day, include_prev=False)["stats"]["focus"]
        if f >= focus_goal:
            streak += 1
        else:
            break
    return streak


def build_insights(con, date_str: str) -> list[str]:
    """Plain-language observations for the day, compared to a trailing baseline average."""
    bd = _baseline_days()
    today = build_summary(con, date_str, include_prev=False)
    st = today["stats"]
    apps_today = today["apps"]
    goals = _goals()
    roles = today["tag_roles"]
    focus_tags, fun_tags = set(roles["focus"]), set(roles["fun"])

    # Baseline: average daily seconds per app over the prior `bd` days.
    d = dt.date.fromisoformat(date_str)
    app_sum: dict[str, float] = {}
    n = 0
    for i in range(1, bd + 1):
        s = build_summary(con, (d - dt.timedelta(days=i)).isoformat(), include_prev=False)
        n += 1
        for a in s["apps"]:
            app_sum[a["name"]] = app_sum.get(a["name"], 0.0) + a["seconds"]

    out: list[str] = []

    # Headline: today's top 'fun' app vs its own average.
    fun_apps = sorted((a for a in apps_today if any(t in fun_tags for t in a.get("tags", []))),
                      key=lambda a: -a["seconds"])
    if fun_apps and fun_apps[0]["seconds"] >= 600:
        top = fun_apps[0]
        avg = app_sum.get(top["name"], 0.0) / n if n else 0.0
        if avg >= 300 and top["seconds"] >= 1.4 * avg:
            out.append(f"{top['name']}: {_fmt(top['seconds'])} — {top['seconds'] / avg:.1f}× your {bd}-day average.")
        else:
            out.append(f"{top['name']}: {_fmt(top['seconds'])} today.")

    # Focus vs goal.
    if st["focus"] >= goals["focus_seconds"]:
        out.append(f"Focus goal hit: {_fmt(st['focus'])} of focus time. 🎯")
    elif st["focus"] > 0:
        short = goals["focus_seconds"] - st["focus"]
        out.append(f"Focus: {_fmt(st['focus'])} — {_fmt(short)} short of your daily goal.")

    # Best deep-work block.
    if st["deep_longest"] >= 600 and st.get("deep_longest_start"):
        when = dt.datetime.fromtimestamp(st["deep_longest_start"]).strftime("%H:%M")
        out.append(f"Best focus block: {_fmt(st['deep_longest'])} starting {when}.")

    # Most productive hour (from focus-tagged sessions).
    hourly = [0.0] * 24
    for x in today["sessions"]:
        if any(t in focus_tags for t in x.get("tags", [])):
            _bucket_hours(hourly, x["start"], x["end"], today["day_start"])
    peak = max(range(24), key=lambda h: hourly[h])
    if hourly[peak] >= 600:
        out.append(f"Most productive hour: {peak:02d}:00 ({_fmt(hourly[peak])}).")

    # Fun during work hours.
    if st["fun_work"] >= 1800:
        out.append(f"{_fmt(st['fun_work'])} of fun time during work hours (9–18h).")

    return out[:5]


def build_range(con, start_date: str, end_date: str) -> dict:
    """Per-day total/focus for [start_date, end_date], for the month calendar heatmap."""
    a, b = dt.date.fromisoformat(start_date), dt.date.fromisoformat(end_date)
    days = []
    d = a
    while d <= b:
        s = build_summary(con, d.isoformat(), include_prev=False)
        days.append({"date": d.isoformat(), "total": s["total_seconds"],
                     "focus": s["stats"]["focus"]})
        d += dt.timedelta(days=1)
    return {"start": start_date, "end": end_date, "days": days}


def _subkey_entry(summary: dict, kind: str, key: str) -> tuple[float, list, str]:
    """(seconds, tags, display-name) for a 'dir' / 'note' / 'page' key within one day's summary.

    These keys aren't apps or sites — they're the per-app/per-site breakdowns build_summary
    nests under their parent (Warp directories, Obsidian notes, a domain's subpages). Returns
    zeros if the key has no time that day."""
    if kind == "dir":
        app = next((a for a in summary["apps"] if a["bundle_id"] == WARP_BUNDLE), None)
        e = next((x for x in (app or {}).get("directories", []) if x["dir"] == key), None)
        return (e["seconds"], e["tags"], key) if e else (0.0, [], key)
    if kind == "note":
        app = next((a for a in summary["apps"] if a["bundle_id"] == OBSIDIAN_BUNDLE), None)
        e = next((x for x in (app or {}).get("notes", []) if x["note"] == key), None)
        return (e["seconds"], e["tags"], key) if e else (0.0, [], key)
    for w in summary["websites"]:  # page: a subpage nested under its domain
        for sp in w.get("subpages", []):
            if sp["page"] == key:
                return sp["seconds"], sp["tags"], sp.get("label") or key
    return (0.0, [], key)


def build_detail(con, kind: str, key: str, date_str: str, days: int = 14) -> dict:
    """History + sessions + hourly distribution for one app (bundle id), site (domain), Warp
    directory ('dir'), Obsidian note ('note') or browser subpage ('page')."""
    d = dt.date.fromisoformat(date_str)
    history = []
    for i in range(days - 1, -1, -1):
        s = build_summary(con, (d - dt.timedelta(days=i)).isoformat(), include_prev=False)
        if kind == "app":
            secs = sum(a["seconds"] for a in s["apps"] if a["bundle_id"] == key)
        elif kind == "site":
            secs = sum(w["seconds"] for w in s["websites"] if w["domain"] == key)
        else:
            secs = _subkey_entry(s, kind, key)[0]
        history.append({"date": (d - dt.timedelta(days=i)).isoformat(), "seconds": secs})

    today = build_summary(con, date_str, include_prev=False)
    start, end = day_bounds(date_str)
    hourly = [0.0] * 24
    if kind == "app":
        sess = [x for x in today["sessions"] if x["bundle_id"] == key]
        name = next((a["name"] for a in today["apps"] if a["bundle_id"] == key), key)
        tags = next((a["tags"] for a in today["apps"] if a["bundle_id"] == key), [])
        for x in sess:
            _bucket_hours(hourly, x["start"], x["end"], start)
        sessions_count = len(sess)
    elif kind == "site":
        name = key
        w = next((w for w in today["websites"] if w["domain"] == key), None)
        tags = w["tags"] if w else []
        sessions_count = w["sessions"] if w else 0
        for ts, interval in con.execute(
            "SELECT ts, interval FROM mac_samples WHERE domain = ? AND idle = 0 AND ts >= ? AND ts < ?",
            (key, start, end),
        ):
            _bucket_hours(hourly, ts, ts + interval, start)
    else:
        # dir/note/page keys aren't DB columns — re-derive today's hourly + session count from
        # raw Mac samples using the same resolver build_summary applies (project_root /
        # obsidian_note / canonical_url). A session breaks when the gap exceeds one interval.
        _secs, tags, name = _subkey_entry(today, kind, key)
        if kind == "dir":
            rows = con.execute(
                "SELECT ts, interval, window_title FROM mac_samples"
                " WHERE bundle_id = ? AND idle = 0 AND ts >= ? AND ts < ? ORDER BY ts",
                (WARP_BUNDLE, start, end))
            resolve = project_root
        elif kind == "note":
            rows = con.execute(
                "SELECT ts, interval, window_title FROM mac_samples"
                " WHERE bundle_id = ? AND idle = 0 AND ts >= ? AND ts < ? ORDER BY ts",
                (OBSIDIAN_BUNDLE, start, end))
            resolve = obsidian_note
        else:  # page
            rows = con.execute(
                "SELECT ts, interval, url FROM mac_samples"
                " WHERE url IS NOT NULL AND idle = 0 AND ts >= ? AND ts < ? ORDER BY ts",
                (start, end))
            resolve = canonical_url
        sessions_count = 0
        prev_end: int | None = None
        for ts, interval, v in rows:
            if resolve(v) != key:
                continue
            _bucket_hours(hourly, ts, ts + interval, start)
            if prev_end is None or ts - prev_end > interval:
                sessions_count += 1
            prev_end = ts + interval

    return {
        "kind": kind, "key": key, "name": name, "tags": tags,
        "history": history, "hourly": [round(h) for h in hourly],
        "sessions": sessions_count, "today_seconds": history[-1]["seconds"] if history else 0,
    }
