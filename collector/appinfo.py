#!/usr/bin/env python3
"""Friendly app names, URL→domain, and iPhone-vs-Watch device classification.

Friendly names are best-effort: a small curated map for common apps, otherwise the last
component of the bundle id (``com.burbn.instagram`` → ``instagram``). The dashboard can
show whatever it likes; this just makes totals readable.
"""
from __future__ import annotations

import glob
import json
import os
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

# Curated names for apps seen on Saad's devices. Extend freely.
FRIENDLY = {
    "com.zhiliaoapp.musically": "TikTok",
    "net.whatsapp.WhatsApp": "WhatsApp",
    "com.burbn.instagram": "Instagram",
    "com.apple.mobilesafari": "Safari",
    "com.apple.Safari": "Safari",
    "com.spotify.client": "Spotify",
    "com.google.gemini": "Gemini",
    "com.anthropic.claude": "Claude",
    "com.mobilefootie.fotmobpro": "FotMob",
    "com.kicktipp.kicktippiphone": "Kicktipp",
    "com.ubercab.UberEats": "Uber Eats",
    "com.google.ios.youtube": "YouTube",
    "com.google.Maps": "Google Maps",
    "com.apple.mobilemail": "Mail",
    "com.apple.mobilenotes": "Notes",
    "com.apple.Health": "Health",
    "com.apple.SleepLockScreen": "Lock Screen",
    "com.apple.springboard.today-view": "Home Screen",
    "com.amazon.AmazonDE": "Amazon",
    "de.mcdonalds.McDonaldsInfoApp": "McDonald's",
    "ai.perplexity.comet": "Comet",
    "com.google.Chrome": "Chrome",
    "com.apple.dt.Xcode": "Xcode",
    "md.obsidian": "Obsidian",
    "dev.warp.Warp-Stable": "Warp",
}

# Bundle-id fragments that mark a synced device as an Apple Watch (vs iPhone).
_WATCH_MARKERS = ("watchkitapp", "nanosleep", "carousel", "heartrate", "com.apple.nano")


def friendly_name(bundle_id: str | None, fallback: str | None = None) -> str:
    """Best display name for an app.

    The curated map wins (it fixes cases like Warp, whose macOS process name is the
    unhelpful "stable"). Otherwise use ``fallback`` (the OS-reported app name, when we have
    one), else the last component of the bundle id.
    """
    if bundle_id and bundle_id in FRIENDLY:
        return FRIENDLY[bundle_id]
    if fallback:
        return fallback
    if not bundle_id:
        return "Unknown"
    return bundle_id.rsplit(".", 1)[-1]


def domain_of(url: str | None) -> str | None:
    """Extract the registrable host from a URL (``https://www.espn.com/...`` → ``espn.com``)."""
    if not url:
        return None
    host = urlsplit(url).hostname
    if not host:
        return None
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def project_root(window_title: str | None) -> str | None:
    """Project-root key from a Warp window title (which the zsh prompt hook sets to the cwd,
    e.g. ``~/CDBS_Control/web``). Returns the top folder under home: ``~/CDBS_Control``.

    Returns ``None`` when the title is not a path — a running program (``npm run dev``, an
    agent task) overrides the title while it runs, and that must not be mistaken for a
    directory. Only ``~``-relative paths have a well-defined project root here; bare absolute
    paths (``/usr/...``) return ``None`` and fall into the caller's "(other)" bucket.
    """
    if not window_title:
        return None
    t = window_title.strip()
    if not t.startswith("~"):  # not a home-relative path → not a recognised project dir
        return None
    rest = t[1:].lstrip("/")
    if not rest:
        return "~"  # the home directory itself
    return f"~/{rest.split('/', 1)[0]}"


_CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")


def _abs_to_home(path: str) -> str | None:
    """``/Users/saad/CDBS_Control/web`` → ``~/CDBS_Control/web``. ``None`` if not under home
    (the dir keys used elsewhere are all home-relative, so a path outside home is unusable)."""
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return None


def _first_cwd(path: str) -> str | None:
    """``cwd`` from the first JSONL record that carries one. It is constant for a session, so
    we read only the head of the file — a multi-MB log costs nothing."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for _ in range(50):  # cwd is on the first record; cap the scan defensively
                line = fh.readline()
                if not line:
                    break
                try:
                    c = json.loads(line).get("cwd")
                except json.JSONDecodeError:
                    continue
                if c:
                    return c
    except OSError:
        return None
    return None


def claude_active_dir(max_age_s: float = 60.0, now: float | None = None) -> str | None:
    """Home-relative cwd of the Claude Code session running right now, or ``None``.

    A Claude Code session in Warp owns the terminal title while it runs (a task name or
    spinner glyph, never the cwd), so ``project_root()`` sees no path and the sample buckets
    as "(other)". But each session continuously rewrites a log at
    ``~/.claude/projects/<slug>/<id>.jsonl`` that carries the literal ``cwd``. The freshest
    log within ``max_age_s`` is the session active now; its cwd is the Warp pane's directory.
    Stale logs (idle or closed sessions) are ignored so a non-Claude program is never
    misattributed to an old session's folder. Only the directory is used — the session id and
    name are irrelevant, exactly as wanted."""
    try:
        files = glob.glob(os.path.join(_CLAUDE_PROJECTS, "*", "*.jsonl"))
    except OSError:
        return None
    now = time.time() if now is None else now
    best, best_m = None, 0.0
    for f in files:
        try:
            m = os.path.getmtime(f)
        except OSError:
            continue
        if m > best_m:
            best, best_m = f, m
    if best is None or now - best_m > max_age_s:
        return None
    cwd = _first_cwd(best)
    return _abs_to_home(cwd) if cwd else None


# Query params that carry no page identity — tracking/share noise, stripped everywhere.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "yclid", "mc_cid", "mc_eid",
    "igshid", "ref", "ref_src", "ref_url", "source", "spm", "scid", "si",
}
# Per-host whitelist: for these hosts ONLY these params identify the page; everything else
# (timestamps, playlist, autoplay…) is dropped. Empty set = drop all query params for that
# host (e.g. youtu.be carries the id in the path, so ?t=… is noise).
_KEEP_PARAMS = {
    "youtube.com": {"v"},
    "youtu.be": set(),
}


def canonical_url(url: str | None) -> str | None:
    """Stable per-page key from a URL: ``host + path`` plus the query params that identify
    the page. Drops the scheme, ``www.``, the fragment and tracking/share junk so the same
    page groups together no matter which link opened it. For hosts in ``_KEEP_PARAMS``
    (YouTube) only the listed params survive, so a video is one key regardless of timestamp
    or playlist. The host is lowercased; the path is left as-is (YouTube ids are
    case-sensitive). Used both to group subpages at query time and as the override key.
    """
    if not url:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    keep = _KEEP_PARAMS.get(host)
    pairs = []
    for k, v in parse_qsl(parts.query):
        if keep is not None:
            if k in keep:
                pairs.append((k, v))
        elif k.lower() not in _TRACKING_PARAMS:
            pairs.append((k, v))
    pairs.sort()
    page = host + parts.path.rstrip("/")
    q = urlencode(pairs)
    return f"{page}?{q}" if q else page


def classify_device(bundle_ids: list[str]) -> str:
    """Guess 'watch' vs 'iphone' from a device's app signature (never hardcode GUIDs)."""
    sig = " ".join(b.lower() for b in bundle_ids if b)
    if any(m in sig for m in _WATCH_MARKERS):
        return "watch"
    if sig:
        return "iphone"
    return "unknown"
