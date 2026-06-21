#!/usr/bin/env python3
"""Resolve app / website icons, cached locally so each lookup hits the network at most once.

Privacy: usage data never leaves the machine — only the single key being resolved does, and
only via either a local lookup (Mac apps) or the site/Apple itself:
  * Mac apps  -> the local ``.app`` bundle's ``.icns`` (no network), converted with ``sips``.
  * iOS apps  -> Apple's iTunes lookup by bundle id (one fetch, then cached).
  * websites  -> the site's own ``/favicon.ico`` (no third-party favicon service).

Results land in ``data/icons/<hash>.<ext>``; a ``.miss`` marker records a failed lookup so we
don't retry it every page load. The dashboard falls back to a letter-tile on a 404.
"""
from __future__ import annotations

import hashlib
import json
import plistlib
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / "data" / "icons"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def get_icon(kind: str, key: str) -> tuple[bytes, str] | None:
    """Return (bytes, content_type) for an app/site icon, or None if unresolved."""
    CACHE.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha1(f"{kind}:{key}".encode()).hexdigest()
    for ext, ctype in (("png", "image/png"), ("ico", "image/x-icon")):
        p = CACHE / f"{h}.{ext}"
        if p.exists():
            return p.read_bytes(), ctype
    if (CACHE / f"{h}.miss").exists():
        return None

    if kind == "app":
        data = _mac_app_icon(key) or _ios_app_icon(key)
        ext, ctype = "png", "image/png"
    else:
        data = _site_favicon(key)
        is_png = bool(data) and data[:8] == _PNG_MAGIC
        ext, ctype = ("png", "image/png") if is_png else ("ico", "image/x-icon")

    if data:
        (CACHE / f"{h}.{ext}").write_bytes(data)
        return data, ctype
    (CACHE / f"{h}.miss").write_bytes(b"")
    return None


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


_APP_DIRS = [
    Path("/Applications"), Path("/System/Applications"),
    Path("/System/Applications/Utilities"), Path.home() / "Applications",
]


def _find_app(bundle: str) -> Path | None:
    """Locate an .app by bundle id: Spotlight first, then scan standard app folders."""
    r = _run(["mdfind", f"kMDItemCFBundleIdentifier == '{bundle}'"], 5)
    if r:
        for line in r.stdout.splitlines():
            if line.endswith(".app"):
                return Path(line)
    # Spotlight may not index bundle ids on this machine — scan dirs and read each Info.plist.
    for d in _APP_DIRS:
        if not d.exists():
            continue
        for app in d.glob("*.app"):
            try:
                bid = plistlib.loads((app / "Contents" / "Info.plist").read_bytes()).get("CFBundleIdentifier")
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
            if bid == bundle:
                return app
    return None


def _mac_app_icon(bundle: str) -> bytes | None:
    app = _find_app(bundle)
    if app is None:
        return None
    res = app / "Contents" / "Resources"
    icns = None
    try:
        name = plistlib.loads((app / "Contents" / "Info.plist").read_bytes()).get("CFBundleIconFile")
        if name:
            cand = res / (name if name.endswith(".icns") else name + ".icns")
            if cand.exists():
                icns = cand
    except (OSError, ValueError, plistlib.InvalidFileException):
        pass
    if icns is None and res.exists():
        found = sorted(res.glob("*.icns"))
        icns = found[0] if found else None
    if icns is None:
        return None
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        out = Path(tmp.name)
    try:
        _run(["sips", "-s", "format", "png", "-Z", "128", str(icns), "--out", str(out)], 10)
        return out.read_bytes() if out.stat().st_size else None
    except OSError:
        return None
    finally:
        out.unlink(missing_ok=True)


def _fetch(url: str, timeout: float) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:  # noqa: BLE001 — any network/parse failure → fall back to letter-tile
        return None


def _ios_app_icon(bundle: str) -> bytes | None:
    q = urllib.parse.quote(bundle)
    raw = _fetch(f"https://itunes.apple.com/lookup?bundleId={q}&entity=software", 6)
    if not raw:
        return None
    try:
        results = json.loads(raw).get("results") or []
    except json.JSONDecodeError:
        return None
    if not results:
        return None
    art = results[0].get("artworkUrl100") or results[0].get("artworkUrl60")
    return _fetch(art, 6) if art else None


def _site_favicon(domain: str) -> bytes | None:
    data = _fetch(f"https://{domain}/favicon.ico", 6)
    return data if data and len(data) > 50 else None
