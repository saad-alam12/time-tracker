#!/usr/bin/env python3
"""Mac live foreground sampler.

Every ``poll_interval_seconds`` it records the frontmost app (bundle id, name, window
title), and — when that app is a configured browser (Comet/Chrome) — its active-tab URL
via AppleScript. It marks a sample ``idle`` when the user has been inactive longer than
``idle_threshold_seconds`` (so AFK time is stored but easy to exclude).

macOS-only. Uses ``osascript`` (AppleScript) and ``ioreg`` via subprocess — no third-party
deps. The first AppleScript call against Comet/System Events triggers one-time Automation
and Accessibility permission prompts that Saad must approve.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from appinfo import claude_active_dir, domain_of, project_root
from db import connect, insert_mac_sample

_CONFIG = Path(__file__).resolve().parent.parent / "config" / "settings.json"
_WARP_BUNDLE = "dev.warp.Warp-Stable"  # only app with a per-directory drill-down

# One AppleScript round-trip for the frontmost app's bundle id, name and window title.
# Window title needs Accessibility permission; we degrade to "" if it's unavailable.
_FRONT_SCRIPT = r'''
tell application "System Events"
    set p to first application process whose frontmost is true
    set bid to bundle identifier of p
    set nm to name of p
    try
        set t to value of attribute "AXTitle" of front window of p
    on error
        set t to ""
    end try
end tell
return bid & "|||" & nm & "|||" & t
'''


def _osascript(script: str, timeout: float = 5.0) -> str | None:
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def frontmost() -> tuple[str | None, str | None, str | None]:
    """Return (bundle_id, app_name, window_title) for the frontmost app."""
    res = _osascript(_FRONT_SCRIPT)
    if not res:
        return None, None, None
    parts = res.split("|||")
    while len(parts) < 3:
        parts.append("")
    bid, name, title = parts[0] or None, parts[1] or None, parts[2] or None
    return bid, name, title


def browser_tab(bundle_id: str) -> tuple[str | None, str | None]:
    """(active-tab URL, tab title) for a Chromium browser, or (None, None) (e.g. private window).

    One AppleScript round-trip returns both, joined by ``|||``. The title is the page title
    (e.g. a YouTube video name) — Comet's window AXTitle comes back empty, so the tab's own
    ``title`` is the only reliable source for per-page labels.
    """
    app_name = {"ai.perplexity.comet": "Comet", "com.google.Chrome": "Google Chrome"}.get(bundle_id)
    if not app_name:
        return None, None
    res = _osascript(
        f'tell application "{app_name}" to get (URL of active tab of front window)'
        f' & "|||" & (title of active tab of front window)'
    )
    if not res:
        return None, None
    url, _, title = res.partition("|||")
    return (url or None), (title or None)


def idle_seconds() -> float:
    """Seconds since the last user input (keyboard/mouse), via IOKit HID idle time."""
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=5
        ).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 0.0
    for line in out.splitlines():
        if "HIDIdleTime" in line:
            try:
                return int(line.split("=")[-1].strip()) / 1_000_000_000  # nanoseconds → s
            except ValueError:
                return 0.0
    return 0.0


def load_settings() -> dict:
    return json.loads(_CONFIG.read_text())


# The tracker's own dashboard — viewing it should never count as usage.
_DASHBOARD_PORTS = {8765, 8766}


def is_dashboard(url: str | None) -> bool:
    """True if a browser URL is the local Time Tracker dashboard."""
    if not url:
        return False
    p = urlsplit(url)
    return p.hostname in ("127.0.0.1", "localhost") and p.port in _DASHBOARD_PORTS


def run() -> None:
    cfg = load_settings()
    interval = int(cfg["poll_interval_seconds"])
    idle_threshold = int(cfg["idle_threshold_seconds"])
    browsers = set(cfg.get("browser_bundle_ids", []))
    con = connect()
    last: tuple | None = None  # last non-dashboard sample, to carry forward over the dashboard
    while True:
        start = time.time()
        bid, name, title = frontmost()
        idle = idle_seconds() >= idle_threshold
        url, page_title = browser_tab(bid) if (bid in browsers and not idle) else (None, None)
        if is_dashboard(url):
            # Looking at the tracker's own dashboard: credit whatever was active before it
            # (so opening the dashboard never registers as usage). Skip until we have one.
            if last is None:
                time.sleep(max(0.0, interval - (time.time() - start)))
                continue
            bid, name, window_title, url, domain = last
        else:
            window_title = page_title or title
            # Warp running a program (Claude Code, npm…) owns the title, so it holds no cwd
            # and the sample would bucket as "(other)". If a Claude Code session is actively
            # writing its log right now, recover its directory and store it as the title — the
            # query-side project_root() then resolves it like a normal prompt cwd.
            if bid == _WARP_BUNDLE and not idle and project_root(window_title) is None:
                d = claude_active_dir(max_age_s=max(60.0, interval * 4))
                if d:
                    window_title = d
            domain = domain_of(url)
            last = (bid, name, window_title, url, domain)
        insert_mac_sample(
            con, ts=int(start), interval=interval, bundle_id=bid, app_name=name,
            window_title=window_title, url=url, domain=domain, idle=idle,
        )
        # Keep a steady cadence even if the AppleScript calls were slow.
        time.sleep(max(0.0, interval - (time.time() - start)))


if __name__ == "__main__":
    run()
