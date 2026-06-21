#!/usr/bin/env python3
"""Map a (bundle_id, domain) pair to a user-defined category.

Two layers, checked in order:
  1. **Time-aware overrides** (``config/overrides.json``) — set from the dashboard. Each is
     ``{type: app|site|page|dir, key, category, since}``: it takes effect for usage at/after ``since``
     only, so changing an app's category mid-day relabels future usage without rewriting the
     past. The newest override for a key wins and persists as the default going forward.
  2. **Static rules** (``config/categories.json``) — bundle id first, then website domain
     (subdomains match a parent rule). Unmatched → the file's ``default``.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

_CONFIG = Path(__file__).resolve().parent.parent / "config" / "categories.json"
_OVERRIDES = Path(__file__).resolve().parent.parent / "config" / "overrides.json"
_TAGS = Path(__file__).resolve().parent.parent / "config" / "tags.json"
_IGNORE = Path(__file__).resolve().parent.parent / "config" / "ignore.json"
_TIMELINE = Path(__file__).resolve().parent.parent / "config" / "timeline.json"

_FAR_FUTURE = 1 << 62  # sentinel "now/latest" when no timestamp is supplied


def load_ignored() -> dict:
    if _IGNORE.exists():
        try:
            return json.loads(_IGNORE.read_text())
        except json.JSONDecodeError:
            return {"ignored": []}
    return {"ignored": []}


def ignored_sets() -> tuple[set[str], set[str]]:
    """Return (ignored app bundle ids, ignored site domains) for query-time filtering."""
    apps: set[str] = set()
    sites: set[str] = set()
    for it in load_ignored().get("ignored", []):
        key = it.get("key")
        if not key:
            continue
        if it.get("type") == "app":
            apps.add(key)
        elif it.get("type") == "site":
            sites.add(key.lower())
    return apps, sites


def set_ignore(target_type: str, key: str, on: bool) -> None:
    """Add (``on=True``) or remove (``on=False``) an app/site from the ignore list.

    Filtering is done at query time, so toggling this never touches the stored DB rows:
    hiding makes past *and* future usage vanish from the dashboard, and unhiding brings it
    all back. Re-adding an already-ignored key is a no-op (deduped).
    """
    if target_type not in ("app", "site"):
        raise ValueError("target_type must be 'app' or 'site'")
    if target_type == "site":
        key = key.lower()
    data = load_ignored()
    kept = [
        it for it in data.get("ignored", [])
        if not (it.get("type") == target_type and it.get("key") == key)
    ]
    if on:
        kept.append({"type": target_type, "key": key})
    data["ignored"] = kept
    _IGNORE.write_text(json.dumps(data, indent=2))


def category_names() -> list[str]:
    """Available category names for the dashboard's picker (rule categories + default)."""
    cfg = json.loads(_CONFIG.read_text())
    names = list(cfg.get("categories", {}).keys())
    default = cfg.get("default", "Uncategorised")
    if default not in names:
        names.append(default)
    # Include any categories that exist only as overrides (user-created on the fly).
    for o in _load_overrides().get("overrides", []):
        if o.get("category") and o["category"] not in names:
            names.append(o["category"])
    return names


def rename_category(old: str, new: str) -> str:
    """Rename a category everywhere it is stored as a name: the rule file (its key and the
    'default'), every override, and the timeline lane lists. Categories are derived at query
    time, so this relabels past and future data alike — no DB change. settings.json's
    category_roles is renamed separately by queries.rename_category_role(). Returns the new
    name. Rejects an empty name or a collision with an existing category (to avoid an
    accidental merge)."""
    new = new.strip()
    if not new:
        raise ValueError("new name required")
    if new == old:
        return new
    existing = {n.lower() for n in category_names() if n != old}
    if new.lower() in existing:
        raise ValueError(f"a category named '{new}' already exists")

    cfg = json.loads(_CONFIG.read_text())
    cats = cfg.get("categories", {})
    if old in cats:  # preserve key order while swapping the one key
        cfg["categories"] = {(new if k == old else k): v for k, v in cats.items()}
    if cfg.get("default") == old:
        cfg["default"] = new
    _CONFIG.write_text(json.dumps(cfg, indent=2))

    ov = _load_overrides()
    for o in ov.get("overrides", []):
        if o.get("category") == old:
            o["category"] = new
    _OVERRIDES.write_text(json.dumps(ov, indent=2))

    tl = load_timeline_prefs()
    save_timeline_prefs(*[[new if c == old else c for c in tl[k]] for k in ("order", "shown", "hidden")])
    return new


def delete_category(name: str) -> str:
    """Delete a category everywhere it is stored as a name: drop its rule from the rule file
    and its lane from the timeline lists, and rewrite every override pinned to it to the
    file's ``default`` (Uncategorised) so those items become uncategorised rather than
    silently re-matching another rule. Categories are derived at query time, so this relabels
    past and future data alike — no DB change. settings.json's category_roles and
    tag_categories are cleaned separately by queries.remove_category_role()/
    remove_tag_category(). Returns the default name items fell back to. Refuses to delete the
    default category itself (it is the fallback)."""
    cfg = json.loads(_CONFIG.read_text())
    default = cfg.get("default", "Uncategorised")
    if name == default:
        raise ValueError("cannot delete the default category")
    cfg.get("categories", {}).pop(name, None)
    _CONFIG.write_text(json.dumps(cfg, indent=2))

    ov = _load_overrides()
    for o in ov.get("overrides", []):
        if o.get("category") == name:
            o["category"] = default
    _OVERRIDES.write_text(json.dumps(ov, indent=2))

    tl = load_timeline_prefs()
    save_timeline_prefs(*[[c for c in tl[k] if c != name] for k in ("order", "shown", "hidden")])
    return default


def _load_overrides() -> dict:
    if _OVERRIDES.exists():
        try:
            return json.loads(_OVERRIDES.read_text())
        except json.JSONDecodeError:
            return {"overrides": []}
    return {"overrides": []}


def load_timeline_prefs() -> dict:
    """Activity-timeline display prefs (swimlane only): lane order, force-shown, force-hidden."""
    if _TIMELINE.exists():
        try:
            data = json.loads(_TIMELINE.read_text())
            return {"order": list(data.get("order", [])),
                    "shown": list(data.get("shown", [])),
                    "hidden": list(data.get("hidden", []))}
        except json.JSONDecodeError:
            pass
    return {"order": [], "shown": [], "hidden": []}


def save_timeline_prefs(order: list, shown: list, hidden: list) -> dict:
    """Persist the timeline lane order, force-shown lanes, and hidden lanes. Returns the prefs."""
    prefs = {"order": [str(c) for c in order],
             "shown": [str(c) for c in shown],
             "hidden": [str(c) for c in hidden]}
    data = {
        "_comment": "Activity-timeline display prefs (swimlane only — does NOT change totals or "
                    "data). A category is a lane if it is not in 'hidden' AND (has data that day "
                    "OR is in 'shown'); everything else is a chip. 'order' is the top-to-bottom "
                    "lane order. Set via the dashboard (drag to reorder, ✕ to remove, click a chip "
                    "to add) or edit here.",
        **prefs,
    }
    _TIMELINE.write_text(json.dumps(data, indent=2))
    return prefs


def add_override(target_type: str, key: str, category: str, since: int | None = None) -> int:
    """Append a category override effective from ``since`` (defaults to now). Returns ``since``."""
    if target_type not in ("app", "site", "page", "dir"):
        raise ValueError("target_type must be 'app', 'site', 'page' or 'dir'")
    if target_type == "site":
        key = key.lower()  # page/dir keys are left as-is — paths/ids are case-sensitive
    since = int(since if since is not None else time.time())
    data = _load_overrides()
    data.setdefault("overrides", []).append(
        {"type": target_type, "key": key, "category": category, "since": since}
    )
    _OVERRIDES.write_text(json.dumps(data, indent=2))
    return since


# --- Tags: a second, orthogonal label layer ---------------------------------------------
# A category is exactly one per time-chunk (the doughnut). A TAG cross-cuts categories: the
# same chunk can carry several. "Work" tags both Warp/CDBS_Control (a Coding chunk) and the
# company docs site (not Coding), so per-tag totals overlap the category totals — shown as
# overlapping rings + a per-category hover breakdown. Same time-aware, query-time philosophy
# as overrides: assignments are append-only with a `since` and an `on` flag (so a tag can be
# removed), and the latest entry at/before a timestamp decides membership.

def _load_tags() -> dict:
    if _TAGS.exists():
        try:
            return json.loads(_TAGS.read_text())
        except json.JSONDecodeError:
            return {"tags": []}
    return {"tags": []}


def tag_names() -> list[str]:
    """Distinct tag names ever assigned, for the dashboard's tag picker."""
    seen: list[str] = []
    for t in _load_tags().get("tags", []):
        nm = t.get("tag")
        if nm and nm not in seen:
            seen.append(nm)
    return seen


def add_tag(target_type: str, key: str, tag: str, on: bool = True, since: int | None = None) -> int:
    """Add (``on=True``) or remove (``on=False``) a tag on an app/site/page/dir key, effective
    from ``since`` (defaults to now). Append-only and time-aware, so past stays consistent.
    Returns ``since``."""
    if target_type not in ("app", "site", "page", "dir"):
        raise ValueError("target_type must be 'app', 'site', 'page' or 'dir'")
    tag = tag.strip()
    if not tag:
        raise ValueError("tag required")
    if target_type == "site":
        key = key.lower()
    # Tags are retroactive by default (since=0): a tag describes what a thing *is*, so it
    # covers all of that key's past and future time. A later add/remove (also since=0) wins by
    # append order. Pass an explicit `since` to time-window instead.
    since = int(since if since is not None else 0)
    data = _load_tags()
    data.setdefault("tags", []).append(
        {"type": target_type, "key": key, "tag": tag, "on": bool(on), "since": since}
    )
    _TAGS.write_text(json.dumps(data, indent=2))
    return since


class Categorizer:
    def __init__(self, path: Path = _CONFIG):
        cfg = json.loads(path.read_text())
        self.default = cfg.get("default", "Uncategorised")
        self._by_bundle: dict[str, str] = {}
        self._by_domain: dict[str, str] = {}
        for cat, rules in cfg.get("categories", {}).items():
            for b in rules.get("bundles", []):
                self._by_bundle[b] = cat
            for d in rules.get("domains", []):
                self._by_domain[d.lower()] = cat
        # Overrides indexed by key, kept sorted by 'since' so the latest active one wins.
        self._ov_app: dict[str, list[tuple[int, str]]] = {}
        self._ov_site: dict[str, list[tuple[int, str]]] = {}
        self._ov_page: dict[str, list[tuple[int, str]]] = {}  # canonical-url -> overrides
        self._ov_dir: dict[str, list[tuple[int, str]]] = {}   # project-root path -> overrides
        for o in _load_overrides().get("overrides", []):
            t = o.get("type")
            tgt = (self._ov_app if t == "app" else self._ov_page if t == "page"
                   else self._ov_dir if t == "dir" else self._ov_site)
            tgt.setdefault(o["key"], []).append((int(o["since"]), o["category"]))
        for d in (self._ov_app, self._ov_site, self._ov_page):
            for lst in d.values():
                lst.sort()
        # Dir overrides are retroactive (since=0), so sort by `since` only — a stable sort
        # keeps append order for equal `since`, letting the newest assignment win.
        for lst in self._ov_dir.values():
            lst.sort(key=lambda x: x[0])

    @staticmethod
    def _active(lst: list[tuple[int, str]] | None, ts: int) -> str | None:
        """Most recent override whose 'since' is at or before ts."""
        if not lst:
            return None
        for since, cat in reversed(lst):
            if since <= ts:
                return cat
        return None

    def _active_dir(self, dir_key: str, ts: int) -> str | None:
        """Longest dir-override prefix active at ts. An override on ``~/CDBS_Control`` applies
        to that root and any ``~/CDBS_Control/...`` subfolder (mirrors the domain→subdomain
        walk). Aggregation collapses samples to a root before calling, so dir_key==prefix is
        the usual case; prefix matching keeps it correct if granularity ever deepens."""
        best: tuple[int, str] | None = None
        for prefix, lst in self._ov_dir.items():
            if dir_key == prefix or dir_key.startswith(prefix + "/"):
                c = self._active(lst, ts)
                if c is not None and (best is None or len(prefix) > best[0]):
                    best = (len(prefix), c)
        return best[1] if best else None

    def categorize(self, bundle_id: str | None, domain: str | None, ts: int | None = None,
                   page: str | None = None, dir_key: str | None = None) -> str:
        t = _FAR_FUTURE if ts is None else ts
        # 0. Directory override (Warp project root) — most specific signal. dir_key is only
        #    ever passed for Warp samples, so this cannot affect any other app; within Warp it
        #    must beat the Warp app override (so e.g. ~/Time Tracker can be Personal while
        #    other Warp dirs fall back to Warp's uncategorised default).
        if dir_key:
            c = self._active_dir(dir_key, t)
            if c:
                return c
        # 1. Overrides active at this time, most specific first: app, then exact page
        #    (a single subpage the user re-categorised), then site, then static rules.
        if bundle_id:
            c = self._active(self._ov_app.get(bundle_id), t)
            if c:
                return c
        if page:
            c = self._active(self._ov_page.get(page), t)
            if c:
                return c
        if domain:
            c = self._active(self._ov_site.get(domain.lower()), t)
            if c:
                return c
        # 2. Static rules.
        if bundle_id and bundle_id in self._by_bundle:
            return self._by_bundle[bundle_id]
        if domain:
            domain = domain.lower()
            parts = domain.split(".")
            for i in range(len(parts) - 1):
                cand = ".".join(parts[i:])
                if cand in self._by_domain:
                    return self._by_domain[cand]
        return self.default


class Tagger:
    """Resolves the SET of tags active on a time-chunk (orthogonal to its one category).

    Each tag assignment is ``{type, key, tag, on, since}``; for a given (type, key, tag) the
    latest entry at/before ts decides on/off. ``dir`` tags cascade to subfolders by
    longest-prefix (mirrors the category dir override), so tagging ``~/CDBS_Control`` Work
    also tags ``~/CDBS_Control/web``."""

    def __init__(self):
        # member-key -> tag -> sorted [(since, on)]; dir kept separate for prefix matching.
        self._exact: dict[tuple[str, str], dict[str, list[tuple[int, bool]]]] = {}
        self._dir: dict[str, dict[str, list[tuple[int, bool]]]] = {}
        for a in _load_tags().get("tags", []):
            t, key, tag = a.get("type"), a.get("key"), a.get("tag")
            if not (t and key and tag):
                continue
            store = self._dir.setdefault(key, {}) if t == "dir" \
                else self._exact.setdefault((t, key), {})
            store.setdefault(tag, []).append((int(a["since"]), bool(a.get("on", True))))
        for d in (self._exact, self._dir):
            for tags in d.values():
                for lst in tags.values():
                    lst.sort(key=lambda x: x[0])  # stable: equal `since` keeps append (chronological) order

    @staticmethod
    def _on(lst: list[tuple[int, bool]], ts: int) -> bool:
        """Whether the latest assignment at/before ts turned the tag on."""
        for since, on in reversed(lst):
            if since <= ts:
                return on
        return False

    def _collect(self, store: dict[str, list[tuple[int, bool]]] | None, ts: int, out: set[str]):
        if store:
            for tag, lst in store.items():
                if self._on(lst, ts):
                    out.add(tag)

    def tags_for(self, bundle_id: str | None = None, domain: str | None = None,
                 page: str | None = None, dir_key: str | None = None,
                 ts: int | None = None) -> set[str]:
        t = _FAR_FUTURE if ts is None else ts
        out: set[str] = set()
        if bundle_id:
            self._collect(self._exact.get(("app", bundle_id)), t, out)
        if page:
            self._collect(self._exact.get(("page", page)), t, out)
        if domain:
            self._collect(self._exact.get(("site", domain.lower())), t, out)
        if dir_key:
            # Every matching prefix contributes; the longest match for each tag wins on/off.
            best: dict[str, tuple[int, bool]] = {}
            for prefix, tags in self._dir.items():
                if dir_key == prefix or dir_key.startswith(prefix + "/"):
                    for tag, lst in tags.items():
                        on = self._on(lst, t)
                        if tag not in best or len(prefix) > best[tag][0]:
                            best[tag] = (len(prefix), on)
            for tag, (_plen, on) in best.items():
                if on:
                    out.add(tag)
        return out
