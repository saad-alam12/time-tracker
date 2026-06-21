#!/usr/bin/env python3
"""Build the dashboard's day summary from the database.

Unifies two sources into one view of a single calendar day (local time):
  * Mac live samples — each non-idle row counts ``interval`` seconds for its app; rows with
    a browser URL also count toward that website's domain.
  * Device focus events (iPhone/Watch) — an app is in focus from its event until the next
    event on that device; gaps longer than ``device_gap_cap_seconds`` are treated as the
    screen being off and not counted.

Returns per-app totals (split by source), per-website totals, per-category totals, and a
half-hourly timeline broken down by category.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

from appinfo import canonical_url, friendly_name, project_root  # noqa: E402
from categorize import Categorizer, Tagger, ignored_sets, load_ignored, load_timeline_prefs  # noqa: E402

_SETTINGS = Path(__file__).resolve().parent.parent / "config" / "settings.json"

WARP_BUNDLE = "dev.warp.Warp-Stable"  # only app with a per-directory drill-down

BUCKET_SECONDS = 1800  # half-hour timeline buckets → 48 per day
N_BUCKETS = 86400 // BUCKET_SECONDS

# Built-in defaults for which categories count as Focus / Leisure. Used only when
# settings.json has no "category_roles" key; the dashboard's Roles panel overrides these.
PRODUCTIVE = {"Research", "NEPOMUC", "Uni", "Dev"}  # default Focus set
DEFAULT_LEISURE = {"Leisure"}
# Work hours window (local) used for the "distraction during work" metric.
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
        "focus_seconds": int(g.get("focus_seconds", 14400)),         # 4h
        "leisure_cap_seconds": int(g.get("leisure_cap_seconds", 7200)),  # 2h
        "deep_blocks": int(g.get("deep_blocks", 3)),
    }


def category_roles() -> dict:
    """Which categories count toward Focus vs Leisure (drives the rings, deep-work blocks
    and the leisure-during-work metric). A category in neither list counts toward neither.
    Falls back to built-in defaults only when the key is absent — an explicit empty list
    is honoured. Editable from the dashboard's Roles panel."""
    r = _settings().get("category_roles")
    if not isinstance(r, dict):
        return {"focus": sorted(PRODUCTIVE), "leisure": sorted(DEFAULT_LEISURE)}
    focus = r["focus"] if isinstance(r.get("focus"), list) else sorted(PRODUCTIVE)
    leisure = r["leisure"] if isinstance(r.get("leisure"), list) else sorted(DEFAULT_LEISURE)
    return {"focus": list(focus), "leisure": list(leisure)}


def save_category_roles(focus: list[str], leisure: list[str]) -> dict:
    """Persist Focus/Leisure assignments into settings.json, preserving all other keys.
    A category can't be both — duplicates are dropped, and Focus wins over Leisure."""
    focus = list(dict.fromkeys(focus))
    leisure = [c for c in dict.fromkeys(leisure) if c not in focus]
    cfg = _settings()
    cfg["category_roles"] = {"focus": focus, "leisure": leisure}
    _SETTINGS.write_text(json.dumps(cfg, indent=2) + "\n")
    return cfg["category_roles"]


def rename_category_role(old: str, new: str) -> None:
    """Swap a renamed category's name inside settings.json's category_roles lists, so the
    Focus/Leisure assignment survives a rename. No-op if the key isn't present."""
    r = _settings().get("category_roles")
    if not isinstance(r, dict):
        return
    save_category_roles(
        [new if c == old else c for c in r.get("focus", [])],
        [new if c == old else c for c in r.get("leisure", [])],
    )


def tag_categories() -> list[str]:
    """Categories for which tagging is enabled. The dashboard's +tag control appears only on
    items whose category is in this list (descriptive categories like Music/Messaging don't
    need tags). Absent key → empty list = tags off everywhere until a category is opted in."""
    v = _settings().get("tag_categories")
    return list(v) if isinstance(v, list) else []


def save_tag_categories(cats: list[str]) -> list[str]:
    """Persist the tag-enabled category list into settings.json, preserving other keys."""
    cats = list(dict.fromkeys(cats))
    cfg = _settings()
    cfg["tag_categories"] = cats
    _SETTINGS.write_text(json.dumps(cfg, indent=2) + "\n")
    return cats


def rename_tag_category(old: str, new: str) -> None:
    """Swap a renamed category's name inside settings.json's tag_categories. No-op if absent."""
    v = _settings().get("tag_categories")
    if isinstance(v, list):
        save_tag_categories([new if c == old else c for c in v])


def remove_category_role(name: str) -> None:
    """Drop a deleted category from settings.json's category_roles lists. No-op if absent."""
    r = _settings().get("category_roles")
    if not isinstance(r, dict):
        return
    save_category_roles(
        [c for c in r.get("focus", []) if c != name],
        [c for c in r.get("leisure", []) if c != name],
    )


def remove_tag_category(name: str) -> None:
    """Drop a deleted category from settings.json's tag_categories. No-op if absent."""
    v = _settings().get("tag_categories")
    if isinstance(v, list) and name in v:
        save_tag_categories([c for c in v if c != name])


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


def _spread_to_buckets(buckets: dict, category: str, start: float, end: float, day_start: int) -> None:
    """Add the interval [start, end] to half-hour buckets for one category, splitting at edges."""
    s = max(start, day_start)
    e = min(end, day_start + 86400)
    while s < e:
        b = int((s - day_start) // BUCKET_SECONDS)
        b_end = day_start + (b + 1) * BUCKET_SECONDS
        chunk_end = min(e, b_end)
        key = (b, category)
        buckets[key] = buckets.get(key, 0.0) + (chunk_end - s)
        s = chunk_end


def build_summary(con, date_str: str, include_prev: bool = True) -> dict:
    start, end = day_bounds(date_str)
    cat = Categorizer()
    tgr = Tagger()  # orthogonal tag layer (overlaps categories)
    ign_apps, ign_sites = ignored_sets()  # hidden apps/sites: dropped from every total below

    apps: dict[tuple, float] = {}      # (bundle_id, name, source) -> seconds
    websites: dict[str, float] = {}    # domain -> seconds
    web_meta: dict[str, list] = {}     # domain -> [sessions, first_ts, last_ts]
    subpages: dict[str, dict[str, list]] = {}  # domain -> canonical-url -> [seconds, label]
    directories: dict[str, list] = {}  # Warp project root -> [seconds, sessions, first_ts, last_ts]
    categories: dict[str, float] = {}  # category -> seconds
    tag_totals: dict[str, float] = {}          # tag -> seconds (overlaps categories)
    cat_tags: dict[str, dict[str, float]] = {}  # category -> tag -> seconds (hover breakdown)
    buckets: dict[tuple, float] = {}   # (bucket_index, category) -> seconds
    segments: list[tuple] = []         # (start, end, bundle_id, name, category, source, title) sessions

    # --- Mac live samples ---
    # Ordered by ts so consecutive samples of the same app merge into one focus session.
    rows = con.execute(
        "SELECT ts, interval, bundle_id, app_name, url, domain, window_title FROM mac_samples"
        " WHERE ts >= ? AND ts < ? AND idle = 0 ORDER BY ts",
        (start, end),
    ).fetchall()
    cur: list | None = None      # open app session [start, end, bundle, name, category, source]
    cur_dom: str | None = None   # domain of the currently open browser session (for site sessions)
    cur_dir: str | None = None   # Warp project root of the current sample (for dir sessions)
    for ts, interval, bundle_id, app_name, url, domain, window_title in rows:
        # Skip hidden apps, and hidden sites (a browser sample tagged with an ignored domain
        # is dropped whole — that time belonged to the hidden site).
        if bundle_id in ign_apps or (domain and domain.lower() in ign_sites):
            continue
        secs = float(interval)
        name = friendly_name(bundle_id, app_name)
        _add(apps, (bundle_id, name, "mac"), secs)
        page = canonical_url(url) if url else None
        # For Warp, the window title holds the cwd; resolve its project root so the sample's
        # category can follow a per-directory override (dir_key is None for every other app).
        dk = project_root(window_title) if bundle_id == WARP_BUNDLE else None
        c = cat.categorize(bundle_id, domain, ts, page=page, dir_key=dk)  # dir/page override beats site/rule
        _add(categories, c, secs)
        _spread_to_buckets(buckets, c, ts, ts + secs, start)
        sample_tags = sorted(tgr.tags_for(bundle_id, domain, page, dk, ts))  # overlapping tag layer
        for tg in sample_tags:
            _add(tag_totals, tg, secs)
            _add(cat_tags.setdefault(c, {}), tg, secs)
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
        # Merge contiguous same-app samples (a gap > one interval ends the session).
        if cur and cur[2] == bundle_id and ts - cur[1] <= interval:
            cur[1] = ts + secs
            if window_title:
                cur[6] = window_title  # keep the latest tab/window title seen in the session
        else:
            if cur:
                segments.append(tuple(cur))
            cur = [ts, ts + secs, bundle_id, name, c, "mac", window_title or None, sample_tags]
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
                nm = friendly_name(bundle_id)
                _add(apps, (bundle_id, nm, kind), secs)
                c = cat.categorize(bundle_id, None, seg_start)
                _add(categories, c, secs)
                _spread_to_buckets(buckets, c, seg_start, seg_end, start)
                dev_tags = sorted(tgr.tags_for(bundle_id=bundle_id, ts=seg_start))
                for tg in dev_tags:
                    _add(tag_totals, tg, secs)
                    _add(cat_tags.setdefault(c, {}), tg, secs)
                # no window title on device apps
                segments.append((seg_start, seg_end, bundle_id, nm, c, kind, None, dev_tags))

    # --- Shape the response ---
    # Tag each app with its category, except browsers (Comet): a browser's category depends
    # on the site, not the app, so it's shown per-website instead.
    browsers = set(json.loads(_SETTINGS.read_text()).get("browser_bundle_ids", []))
    # Warp's per-directory breakdown: the project roots worked in that day, each with its
    # current category (a dir override, else Warp's app default). The "(other)" bucket holds
    # time whose title wasn't a path (a program owned the title) — not re-categorisable.
    dir_list = [
        {"dir": dk, "seconds": round(sec), "sessions": sessions,
         "category": cat.categorize(WARP_BUNDLE, None, end,
                                    dir_key=(None if dk == "(other)" else dk)),
         "tags": [] if dk == "(other)" else sorted(tgr.tags_for(dir_key=dk, ts=end))}
        for dk, (sec, sessions, _first, _last) in sorted(directories.items(), key=lambda x: -x[1][0])
        if sec >= 60
    ]
    # The chip shows each app's current category (latest rule/override as of day end).
    app_list = [
        {"bundle_id": b, "name": n, "source": s, "seconds": round(sec),
         "category": None if b in browsers else cat.categorize(b, None, end),
         "tags": sorted(tgr.tags_for(bundle_id=b, ts=end)),
         **({"directories": dir_list} if b == WARP_BUNDLE else {})}
        for (b, n, s), sec in sorted(apps.items(), key=lambda x: -x[1])
    ]
    # Drop fleeting visits: only show sites used for at least 2 minutes.
    web_list = []
    browsing = {"productive": 0.0, "distracting": 0.0, "neutral": 0.0}
    for d, sec in sorted(websites.items(), key=lambda x: -x[1]):
        wc = cat.categorize(None, d, end)
        bucket = "productive" if wc in PRODUCTIVE else "distracting" if wc == "Leisure" else "neutral"
        browsing[bucket] += sec
        if sec >= 120:
            meta = web_meta.get(d, [0, 0, 0])
            # Subpages: individual pages/videos on this domain with ≥3 min that day. Each
            # inherits the domain's category unless it has its own page override.
            # Merge pages sharing the same title first: YouTube is a single-page app, so the
            # active-tab URL sometimes lags as the bare host while the title is already the
            # video — that would otherwise list one video twice. Keep the most specific key.
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
                 "category": cat.categorize(None, d, end, page=e[2]),
                 "tags": sorted(tgr.tags_for(page=e[2], ts=end))}
                for e in sorted(merged.values(), key=lambda x: -x[0])
                if e[0] >= 180
            ]
            web_list.append({
                "domain": d, "seconds": round(sec), "category": wc, "kind": bucket,
                "sessions": meta[0], "first": meta[1], "last": meta[2], "subpages": subs,
                "tags": sorted(tgr.tags_for(domain=d, ts=end)),
            })
    browsing = {k: round(v) for k, v in browsing.items()}
    cat_list = [
        {"category": c, "seconds": round(sec)}
        for c, sec in sorted(categories.items(), key=lambda x: -x[1])
    ]
    cat_names = [c["category"] for c in cat_list]
    timeline = {
        "bucket_seconds": BUCKET_SECONDS,
        "labels": [
            (dt.datetime.fromtimestamp(start) + dt.timedelta(seconds=i * BUCKET_SECONDS)).strftime("%H:%M")
            for i in range(N_BUCKETS)
        ],
        "categories": cat_names,
        "series": {
            c: [round(buckets.get((i, c), 0.0)) for i in range(N_BUCKETS)] for c in cat_names
        },
    }
    stats = _compute_stats(segments, categories, start)
    goals = _goals()
    if include_prev:
        prev_date = (dt.date.fromisoformat(date_str) - dt.timedelta(days=1)).isoformat()
        p = build_summary(con, prev_date, include_prev=False)["stats"]
        stats["prev"] = {"total": p["total"], "focus": p["focus"], "leisure": p["leisure"]}
        stats["focus_streak"] = _focus_streak(con, date_str, goals["focus_seconds"])

    # Day-clipped sessions for the swimlane timeline (and drill-downs), earliest first.
    sessions = [
        {"start": int(max(s, start)), "end": int(min(e, end)),
         "bundle_id": b, "name": n, "category": c, "source": src, "title": t, "tags": tg}
        for (s, e, b, n, c, src, t, tg) in sorted(segments, key=lambda r: (r[0], r[1]))
        if min(e, end) > max(s, start)
    ]

    return {
        "date": date_str,
        "total_seconds": round(sum(categories.values())),
        "day_start": start,
        "apps": app_list,
        "websites": web_list,
        "browsing": browsing,
        "categories": cat_list,
        "tags": [{"tag": t, "seconds": round(sec)}
                 for t, sec in sorted(tag_totals.items(), key=lambda x: -x[1])],
        "cat_tags": {c: {t: round(s) for t, s in tags.items()} for c, tags in cat_tags.items()},
        "tag_categories": tag_categories(),  # which categories show the +tag control
        "timeline": timeline,
        "timeline_prefs": load_timeline_prefs(),  # swimlane lane order + hidden lanes
        "sessions": sessions,
        "stats": stats,
        "goals": goals,
        "insights": build_insights(con, date_str) if include_prev else [],
    }


def _compute_stats(segments: list[tuple], categories: dict[str, float], day_start: int) -> dict:
    """Derive the stat-card metrics from the day's focus sessions and category totals."""
    roles = category_roles()
    focus_cats, leisure_cats = set(roles["focus"]), set(roles["leisure"])
    focus = sum(v for k, v in categories.items() if k in focus_cats)
    leisure = sum(v for k, v in categories.items() if k in leisure_cats)

    # Deep-work: merge productive sessions (bridging short gaps) into uninterrupted blocks.
    prod = sorted((s, e) for (s, e, _b, _n, c, _src, _t, _tg) in segments if c in focus_cats)
    blocks: list[list[float]] = []
    for s, e in prod:
        if blocks and s - blocks[-1][1] <= DEEP_GAP:
            blocks[-1][1] = max(blocks[-1][1], e)
        else:
            blocks.append([s, e])
    deep = [b for b in blocks if b[1] - b[0] >= DEEP_MIN]
    longest = max(blocks, key=lambda b: b[1] - b[0], default=None)

    # Distraction during work hours (local 9–18h) + number of TikTok/Instagram opens.
    ws, we = day_start + WORK_START * 3600, day_start + WORK_END * 3600
    leisure_work = sum(_overlap(s, e, ws, we) for (s, e, _b, _n, c, _src, _t, _tg) in segments if c in leisure_cats)
    distraction_opens = sum(1 for (_s, _e, b, _n, _c, _src, _t, _tg) in segments if b in DISTRACTION)

    return {
        "total": round(sum(categories.values())),
        "focus": round(focus),
        "leisure": round(leisure),
        "focus_leisure_ratio": round(focus / leisure, 2) if leisure > 0 else None,
        "deep_block_count": len(deep),
        "deep_longest": round(longest[1] - longest[0]) if longest else 0,
        "deep_longest_start": int(longest[0]) if longest else None,
        "leisure_work": round(leisure_work),
        "distraction_opens": distraction_opens,
    }


def build_trend(con, end_date: str, days: int = 7) -> dict:
    """Per-category totals for the last ``days`` days ending on ``end_date`` (inclusive)."""
    d = dt.date.fromisoformat(end_date)
    out, totals = [], {}
    for i in range(days - 1, -1, -1):
        day = (d - dt.timedelta(days=i)).isoformat()
        s = build_summary(con, day, include_prev=False)
        catmap = {c["category"]: c["seconds"] for c in s["categories"]}
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

    # Baseline: average daily seconds per app/category over the prior `bd` days.
    d = dt.date.fromisoformat(date_str)
    app_sum: dict[str, float] = {}
    n = 0
    for i in range(1, bd + 1):
        s = build_summary(con, (d - dt.timedelta(days=i)).isoformat(), include_prev=False)
        n += 1
        for a in s["apps"]:
            app_sum[a["name"]] = app_sum.get(a["name"], 0.0) + a["seconds"]

    out: list[str] = []

    # Headline: today's top leisure app vs its own average.
    leisure_cats = set(category_roles()["leisure"])
    leisure = [a for a in apps_today if a.get("category") in leisure_cats]
    if leisure and leisure[0]["seconds"] >= 600:
        top = leisure[0]
        avg = app_sum.get(top["name"], 0.0) / n if n else 0.0
        if avg >= 300 and top["seconds"] >= 1.4 * avg:
            out.append(f"{top['name']}: {_fmt(top['seconds'])} — {top['seconds'] / avg:.1f}× your {bd}-day average.")
        else:
            out.append(f"{top['name']}: {_fmt(top['seconds'])} today.")

    # Focus vs goal.
    if st["focus"] >= goals["focus_seconds"]:
        out.append(f"Focus goal hit: {_fmt(st['focus'])} of productive time. 🎯")
    elif st["focus"] > 0:
        short = goals["focus_seconds"] - st["focus"]
        out.append(f"Focus: {_fmt(st['focus'])} — {_fmt(short)} short of your daily goal.")

    # Best deep-work block.
    if st["deep_longest"] >= 600 and st.get("deep_longest_start"):
        when = dt.datetime.fromtimestamp(st["deep_longest_start"]).strftime("%H:%M")
        out.append(f"Best focus block: {_fmt(st['deep_longest'])} starting {when}.")

    # Most productive hour (from productive sessions).
    hourly = [0.0] * 24
    for x in today["sessions"]:
        if x["category"] in PRODUCTIVE:
            _bucket_hours(hourly, x["start"], x["end"], today["day_start"])
    peak = max(range(24), key=lambda h: hourly[h])
    if hourly[peak] >= 600:
        out.append(f"Most productive hour: {peak:02d}:00 ({_fmt(hourly[peak])}).")

    # Distraction during work hours.
    if st["leisure_work"] >= 1800:
        out.append(f"{_fmt(st['leisure_work'])} of leisure during work hours (9–18h).")

    return out[:5]


def build_range(con, start_date: str, end_date: str) -> dict:
    """Per-day total/focus/leisure for [start_date, end_date], for the month calendar heatmap."""
    a, b = dt.date.fromisoformat(start_date), dt.date.fromisoformat(end_date)
    days = []
    d = a
    while d <= b:
        s = build_summary(con, d.isoformat(), include_prev=False)
        days.append({"date": d.isoformat(), "total": s["total_seconds"],
                     "focus": s["stats"]["focus"], "leisure": s["stats"]["leisure"]})
        d += dt.timedelta(days=1)
    return {"start": start_date, "end": end_date, "days": days}


def build_detail(con, kind: str, key: str, date_str: str, days: int = 14) -> dict:
    """History + sessions + hourly distribution for one app (bundle id) or site (domain)."""
    d = dt.date.fromisoformat(date_str)
    history = []
    for i in range(days - 1, -1, -1):
        s = build_summary(con, (d - dt.timedelta(days=i)).isoformat(), include_prev=False)
        if kind == "app":
            secs = sum(a["seconds"] for a in s["apps"] if a["bundle_id"] == key)
        else:
            secs = sum(w["seconds"] for w in s["websites"] if w["domain"] == key)
        history.append({"date": (d - dt.timedelta(days=i)).isoformat(), "seconds": secs})

    today = build_summary(con, date_str, include_prev=False)
    start, end = day_bounds(date_str)
    hourly = [0.0] * 24
    if kind == "app":
        sess = [x for x in today["sessions"] if x["bundle_id"] == key]
        name = next((a["name"] for a in today["apps"] if a["bundle_id"] == key), key)
        category = next((a.get("category") for a in today["apps"] if a["bundle_id"] == key), None)
        for x in sess:
            _bucket_hours(hourly, x["start"], x["end"], start)
        sessions_count = len(sess)
    else:
        name = key
        w = next((w for w in today["websites"] if w["domain"] == key), None)
        category = w["category"] if w else None
        sessions_count = w["sessions"] if w else 0
        for ts, interval in con.execute(
            "SELECT ts, interval FROM mac_samples WHERE domain = ? AND idle = 0 AND ts >= ? AND ts < ?",
            (key, start, end),
        ):
            _bucket_hours(hourly, ts, ts + interval, start)

    return {
        "kind": kind, "key": key, "name": name, "category": category,
        "history": history, "hourly": [round(h) for h in hourly],
        "sessions": sessions_count, "today_seconds": history[-1]["seconds"] if history else 0,
    }
