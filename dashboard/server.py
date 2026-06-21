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
  GET /api/categories        -> available category names for the picker
  GET /api/ignored           -> [{type,key,name}, ...] apps/sites hidden from all views
  GET /api/category_roles    -> {focus:[...], leisure:[...]} categories counting toward each ring
  POST /api/categorize       -> {type,key,category}: set a time-aware category override
  POST /api/ignore           -> {type,key,on}: hide (on=true) / unhide (on=false) an app/site
  POST /api/timeline_prefs   -> {order,shown,hidden}: save swimlane lane order + shown/hidden lanes
  POST /api/category_roles   -> {focus:[...], leisure:[...]}: set Focus/Leisure category roles
  POST /api/rename_category  -> {old,new}: rename a category across all config (past+future)
  POST /api/delete_category  -> {name}: delete a category; its items become Uncategorised

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
            self._json(categorize.category_names())
            return

        if path == "/api/tags":
            self._json(categorize.tag_names())
            return

        if path == "/api/ignored":
            self._json(queries.ignored_list())
            return

        if path == "/api/category_roles":
            self._json(queries.category_roles())
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
        if path not in ("/api/categorize", "/api/ignore", "/api/timeline_prefs",
                        "/api/category_roles", "/api/rename_category",
                        "/api/delete_category", "/api/tag", "/api/tag_categories"):
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if path == "/api/rename_category":
                old = (body.get("old") or "").strip()
                if not old:
                    raise ValueError("old name required")
                new = categorize.rename_category(old, body.get("new", ""))
                queries.rename_category_role(old, new)
                queries.rename_tag_category(old, new)
                self._json({"ok": True, "new": new})
                return
            if path == "/api/delete_category":
                name = (body.get("name") or "").strip()
                if not name:
                    raise ValueError("name required")
                default = categorize.delete_category(name)
                queries.remove_category_role(name)
                queries.remove_tag_category(name)
                self._json({"ok": True, "default": default})
                return
            if path == "/api/tag_categories":
                cats = queries.save_tag_categories(body.get("categories", []))
                self._json({"ok": True, "categories": cats})
                return
            if path == "/api/category_roles":
                roles = queries.save_category_roles(
                    body.get("focus", []), body.get("leisure", []))
                self._json({"ok": True, **roles})
                return
            if path == "/api/timeline_prefs":
                prefs = categorize.save_timeline_prefs(
                    body.get("order", []), body.get("shown", []), body.get("hidden", []))
                self._json({"ok": True, **prefs})
                return
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
            category = (body["category"] or "").strip()
            if not category:
                raise ValueError("category required")
            # Dir categories are retroactive (cover past + future); app/site/page stay from-now-on.
            since = categorize.add_override(
                target_type, key, category, since=0 if target_type == "dir" else None)
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            self._send(400, str(e).encode(), "text/plain")
            return
        self._json({"ok": True, "since": since, "category": category})

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
