#!/usr/bin/env python3
"""Tags (the manual label layer) and emergent categories.

Manual classification is done with **tags** (``config/tags.json``): an app/site/page/dir can
carry any number of tags. Each assignment is time-aware — append-only with a ``since`` and an
``on`` flag, and the latest entry at/before a timestamp decides membership — and resolved by
the :class:`Tagger`.

**Categories** are *emergent* (``config/categories.json``): a category is a named **union of
tags**. Its time is the sum of time on chunks whose tag set intersects the category's tags,
so categories may overlap and their totals can exceed the day total. Categories are derived
at query time (see ``queries.build_summary``) and edited from the dashboard.
"""
from __future__ import annotations

import json
from pathlib import Path

_CONFIG = Path(__file__).resolve().parent.parent / "config" / "categories.json"   # category defs
_TAGS = Path(__file__).resolve().parent.parent / "config" / "tags.json"
_IGNORE = Path(__file__).resolve().parent.parent / "config" / "ignore.json"
_TIMELINE = Path(__file__).resolve().parent.parent / "config" / "timeline.json"

_FAR_FUTURE = 1 << 62  # sentinel "now/latest" when no timestamp is supplied


# --- Hidden apps/sites (filtered from every view at query time) -------------------------

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


# --- Emergent categories (named unions of tags) -----------------------------------------

_CATS_COMMENT = (
    "Emergent categories: each is a named UNION of tags. A category's daily time is the sum "
    "of time on chunks carrying ANY of its tags, so categories may overlap and their totals "
    "can exceed the day total. Tags are the primary manual label (config/tags.json); "
    "categories are derived from them at query time. Create/edit/delete from the dashboard's "
    "settings (gear) -> Categories."
)


def load_category_defs() -> list[dict]:
    """Emergent category definitions: ``[{"name": str, "tags": [str, ...]}, ...]``."""
    if not _CONFIG.exists():
        return []
    try:
        data = json.loads(_CONFIG.read_text())
    except json.JSONDecodeError:
        return []
    out: list[dict] = []
    for c in data.get("categories", []):
        nm = (c.get("name") or "").strip()
        if nm:
            out.append({"name": nm, "tags": [str(t) for t in c.get("tags", [])]})
    # Alphabetical (case-insensitive) so every menu/listing of categories is sorted and a
    # newly created category slots into place. Per-day category *totals* are re-ranked by
    # time in queries.build_summary, so this only orders the pickers, not the bar chart.
    out.sort(key=lambda c: c["name"].casefold())
    return out


def _save_category_defs(defs: list[dict]) -> None:
    data = {"_comment": _CATS_COMMENT,
            "categories": [{"name": c["name"], "tags": c["tags"]} for c in defs]}
    _CONFIG.write_text(json.dumps(data, indent=2))


def save_category_def(name: str, tags: list[str], old_name: str | None = None) -> dict:
    """Create or update an emergent category. With ``old_name`` (when it differs) this renames
    in place and replaces the tag set. Names are unique case-insensitively. Returns the def."""
    name = name.strip()
    if not name:
        raise ValueError("category name required")
    tags = list(dict.fromkeys(t.strip() for t in tags if t and t.strip()))
    defs = load_category_defs()
    key = (old_name or name).strip().lower()
    for c in defs:  # reject a name that collides with a *different* existing category
        if c["name"].lower() == name.lower() and c["name"].lower() != key:
            raise ValueError(f"a category named '{name}' already exists")
    for c in defs:
        if c["name"].lower() == key:
            c["name"], c["tags"] = name, tags
            break
    else:
        defs.append({"name": name, "tags": tags})
    _save_category_defs(defs)
    return {"name": name, "tags": tags}


def delete_category_def(name: str) -> None:
    """Delete an emergent category by name (case-insensitive). No-op if absent."""
    target = name.strip().lower()
    _save_category_defs([c for c in load_category_defs() if c["name"].lower() != target])


# --- Activity-timeline display prefs (per view: 'tag' | 'category') ----------------------

_TL_VIEWS = ("tag", "category")
_TL_COMMENT = (
    "Activity-timeline display prefs, per view ('tag' | 'category'). Does NOT change totals. "
    "A lane shows if it is not in 'hidden' AND (has data that day OR is in 'shown'); 'order' "
    "is the top-to-bottom lane order. Set via the dashboard (drag, hide, add)."
)


def _load_timeline_raw() -> dict:
    if _TIMELINE.exists():
        try:
            d = json.loads(_TIMELINE.read_text())
            if isinstance(d, dict):
                return d
        except json.JSONDecodeError:
            pass
    return {}


def load_timeline_prefs(view: str = "tag") -> dict:
    """Lane order, force-shown and force-hidden lists for one timeline view."""
    v = _load_timeline_raw().get(view, {})
    if not isinstance(v, dict):
        v = {}
    return {"order": list(v.get("order", [])),
            "shown": list(v.get("shown", [])),
            "hidden": list(v.get("hidden", []))}


def save_timeline_prefs(view: str, order: list, shown: list, hidden: list) -> dict:
    """Persist one view's lane order + shown/hidden, preserving the other view. Returns prefs."""
    if view not in _TL_VIEWS:
        raise ValueError("view must be 'tag' or 'category'")
    data = {k: v for k, v in _load_timeline_raw().items() if k in _TL_VIEWS}
    prefs = {"order": [str(c) for c in order],
             "shown": [str(c) for c in shown],
             "hidden": [str(c) for c in hidden]}
    data[view] = prefs
    _TIMELINE.write_text(json.dumps({"_comment": _TL_COMMENT, **data}, indent=2))
    return prefs


# --- Tags: the manual label layer -------------------------------------------------------
# An app/site/page/dir can carry any number of tags. Assignments are append-only with a
# `since` and an `on` flag (so a tag can be removed), and the latest entry at/before a
# timestamp decides membership — the same time-aware, query-time philosophy used elsewhere.

def _load_tags() -> dict:
    if _TAGS.exists():
        try:
            return json.loads(_TAGS.read_text())
        except json.JSONDecodeError:
            return {"tags": []}
    return {"tags": []}


def tag_names() -> list[str]:
    """Distinct tag names ever assigned, alphabetical (case-insensitive), for the dashboard's
    tag picker and role settings. Sorting at the source means a newly created tag slots into
    place automatically wherever this list is shown."""
    seen: list[str] = []
    for t in _load_tags().get("tags", []):
        nm = t.get("tag")
        if nm and nm not in seen:
            seen.append(nm)
    return sorted(seen, key=str.casefold)


def add_tag(target_type: str, key: str, tag: str, on: bool = True, since: int | None = None) -> int:
    """Add (``on=True``) or remove (``on=False``) a tag on an app/site/page/dir key, effective
    from ``since`` (defaults to retroactive, since=0). Append-only and time-aware. Returns
    ``since``."""
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


class Tagger:
    """Resolves the SET of tags active on a time-chunk.

    Each tag assignment is ``{type, key, tag, on, since}``; for a given (type, key, tag) the
    latest entry at/before ts decides on/off. ``dir`` tags cascade to subfolders by
    longest-prefix, so tagging ``~/CDBS_Control`` also tags ``~/CDBS_Control/web``."""

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
                    lst.sort(key=lambda x: x[0])  # stable: equal `since` keeps append order

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
