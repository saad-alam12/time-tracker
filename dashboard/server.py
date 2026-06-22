#!/usr/bin/env python3
"""Minimal local dashboard server.

Serves the static page and a tiny JSON API over the read-only database. Binds to localhost
only — the data never leaves the machine. No third-party server framework; just
``http.server`` from the standard library.

  GET /                      -> dashboard page
  GET /api/dates             -> ["2026-06-20", ...]  (dates with data, newest first)
  GET /api/summary?date=...  -> day summary (apps, websites, categories, timeline, stats, ...)
  GET /api/trend?date=...    -> last 7 days of per-category totals ending on date
  GET /api/calendar?month=.. -> per-day totals for a month (YYYY-MM), for the calendar heatmap
  GET /api/detail?kind&key&date -> history + sessions + hourly for one app/site
  GET /api/icon?kind&key     -> cached app/site icon (404 if unresolved → letter-tile)
  GET /api/categories        -> [{name, tags:[...]}, ...] emergent category definitions
  GET /api/tags              -> all tag names ever assigned (for the picker + role settings)
  GET /api/ignored           -> [{type,key,name}, ...] apps/sites hidden from all views
  GET /api/tag_roles         -> {focus:[...], productive:[...], fun:[...]} tag roles → rings
  POST /api/tag              -> {type,key,tag,on}: add/remove a tag on an app/site/page/dir
  POST /api/ignore           -> {type,key,on}: hide (on=true) / unhide (on=false) an app/site
  POST /api/timeline_prefs   -> {view,order,shown,hidden}: save a swimlane view's lane prefs
  POST /api/tag_roles        -> {focus,productive,fun}: set which tags count toward each ring
  POST /api/category_def     -> {name,tags,old_name?}: create or edit an emergent category
  POST /api/delete_category  -> {name}: delete an emergent category definition

Run:  python3 dashboard/server.py [port]      (default port 8765)
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

import categorize  # noqa: E402
import db  # noqa: E402
import icons  # noqa: E402
import queries  # noqa: E402

HERE = Path(__file__).resolve().parent
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript"),
    "/style.css": ("style.css", "text/css"),
    "/chart.umd.min.js": ("chart.umd.min.js", "application/javascript"),
    "/fonts/Inter.woff2": ("fonts/Inter.woff2", "font/woff2"),
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj) -> None:
        self._send(200, json.dumps(obj).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        parts = urlsplit(self.path)
        path, qs = parts.path, parse_qs(parts.query)

        if path in STATIC:
            fname, ctype = STATIC[path]
            f = HERE / fname
            if f.exists():
                self._send(200, f.read_bytes(), ctype)
            else:
                self._send(404, b"not found", "text/plain")
            return

        if path == "/api/dates":
            con = db.connect_ro()
            try:
                self._json(queries.available_dates(con))
            finally:
                con.close()
            return

        if path == "/api/summary":
            date = (qs.get("date") or [None])[0]
            con = db.connect_ro()
            try:
                if not date:
                    dates = queries.available_dates(con)
                    date = dates[0] if dates else None
                self._json(queries.build_summary(con, date) if date else {})
            finally:
                con.close()
            return

        if path == "/api/trend":
            date = (qs.get("date") or [None])[0]
            con = db.connect_ro()
            try:
                if not date:
                    dates = queries.available_dates(con)
                    date = dates[0] if dates else None
                self._json(queries.build_trend(con, date) if date else {})
            finally:
                con.close()
            return

        if path == "/api/calendar":
            import calendar as _cal
            month = (qs.get("month") or [None])[0]  # "YYYY-MM"
            con = db.connect_ro()
            try:
                if not month:
                    dates = queries.available_dates(con)
                    month = dates[0][:7] if dates else None
                if not month:
                    self._json({})
                    return
                y, m = (int(x) for x in month.split("-"))
                first = f"{y:04d}-{m:02d}-01"
                last = f"{y:04d}-{m:02d}-{_cal.monthrange(y, m)[1]:02d}"
                self._json(queries.build_range(con, first, last))
            finally:
                con.close()
            return

        if path == "/api/detail":
            kind = (qs.get("kind") or ["app"])[0]
            key = (qs.get("key") or [None])[0]
            date = (qs.get("date") or [None])[0]
            con = db.connect_ro()
            try:
                if not date:
                    dates = queries.available_dates(con)
                    date = dates[0] if dates else None
                if key and date:
                    self._json(queries.build_detail(con, kind, key, date))
                else:
                    self._json({})
            finally:
                con.close()
            return

        if path == "/api/categories":
            self._json(categorize.load_category_defs())
            return

        if path == "/api/tags":
            self._json(categorize.tag_names())
            return

        if path == "/api/ignored":
            self._json(queries.ignored_list())
            return

        if path == "/api/tag_roles":
            self._json(queries.tag_roles())
            return

        if path == "/api/icon":
            kind = (qs.get("kind") or ["app"])[0]
            key = (qs.get("key") or [None])[0]
            res = icons.get_icon(kind, key) if key else None
            if res:
                data, ctype = res
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=86400")  # browser-cache for a day
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send(404, b"no icon", "text/plain")
            return

        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        path = urlsplit(self.path).path
        if path not in ("/api/ignore", "/api/timeline_prefs", "/api/tag", "/api/tag_roles",
                        "/api/category_def", "/api/delete_category"):
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if path == "/api/tag_roles":
                roles = queries.save_tag_roles(
                    body.get("focus", []), body.get("productive", []), body.get("fun", []))
                self._json({"ok": True, **roles})
                return
            if path == "/api/category_def":
                name = (body.get("name") or "").strip()
                if not name:
                    raise ValueError("name required")
                cd = categorize.save_category_def(name, body.get("tags", []), body.get("old_name"))
                self._json({"ok": True, **cd})
                return
            if path == "/api/delete_category":
                name = (body.get("name") or "").strip()
                if not name:
                    raise ValueError("name required")
                categorize.delete_category_def(name)
                self._json({"ok": True})
                return
            if path == "/api/timeline_prefs":
                prefs = categorize.save_timeline_prefs(
                    body.get("view", "tag"), body.get("order", []),
                    body.get("shown", []), body.get("hidden", []))
                self._json({"ok": True, **prefs})
                return
            # Remaining endpoints act on an app/site/page/dir key.
            target_type = body["type"]
            key = body["key"]
            if not key:
                raise ValueError("key required")
            if path == "/api/ignore":
                categorize.set_ignore(target_type, key, bool(body.get("on", True)))
                self._json({"ok": True})
                return
            if path == "/api/tag":
                tag = (body.get("tag") or "").strip()
                on = bool(body.get("on", True))
                since = categorize.add_tag(target_type, key, tag, on)
                self._json({"ok": True, "since": since, "tag": tag, "on": on})
                return
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            self._send(400, str(e).encode(), "text/plain")
            return
        self._send(404, b"not found", "text/plain")

    def log_message(self, *args) -> None:  # silence per-request logging
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Time Tracker dashboard → http://127.0.0.1:{port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
