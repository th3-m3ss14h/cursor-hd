#!/usr/bin/env python3
"""Discover and inspect installed Xcursor themes, including GNOME 51 support."""
from __future__ import annotations

import configparser
import os
from pathlib import Path

from xcur_extract import scan_xcursor_toc
import gnome51 as gnome51_module
from gnome51 import validate_scalable_theme

LOW_RES_THRESHOLD = 32
_DEFAULT_SEARCH_DIRS = ["~/.icons", "~/.local/share/icons", "/usr/share/icons", "/usr/local/share/icons"]
# Kept mutable for callers/tests which provide isolated search roots. The normal
# configuration computes ~/.local/share/icons from XDG_DATA_HOME dynamically.
SEARCH_DIRS = list(_DEFAULT_SEARCH_DIRS)


def _xdg_icons_dir() -> str:
    data_home = os.path.expanduser(os.environ.get("XDG_DATA_HOME", "~/.local/share"))
    return os.path.realpath(os.path.join(data_home, "icons"))


def _home_icons_dir() -> str:
    return os.path.realpath(os.path.expanduser("~/.icons"))


def _within(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath((os.path.realpath(path), os.path.realpath(parent))) == os.path.realpath(parent)
    except (ValueError, OSError):
        return False


def _search_dirs():
    """Yield icon theme search roots once, using the current XDG_DATA_HOME."""
    if SEARCH_DIRS != _DEFAULT_SEARCH_DIRS:
        roots = [os.path.expanduser(root) for root in SEARCH_DIRS]
    else:
        roots = [
            os.path.expanduser("~/.icons"),
            _xdg_icons_dir(),
            "/usr/share/icons",
            "/usr/local/share/icons",
        ]
    seen = set()
    for root in roots:
        path = os.path.realpath(root)
        if path not in seen and os.path.isdir(path):
            seen.add(path)
            yield path


def _theme_display_name(theme_dir):
    for filename in ("index.theme", "cursor.theme"):
        index_path = os.path.join(theme_dir, filename)
        if not os.path.isfile(index_path):
            continue
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        try:
            cp.read(index_path, encoding="utf-8")
            for section in cp.sections():
                if section.lower() == "icon theme" and cp.has_option(section, "Name"):
                    return cp.get(section, "Name", fallback="").strip() or os.path.basename(theme_dir)
        except (configparser.Error, OSError, UnicodeError):
            pass
    return os.path.basename(theme_dir)


def _location_label(theme_dir: str) -> str:
    path = os.path.realpath(theme_dir)
    home_icons = _home_icons_dir()
    xdg_icons = _xdg_icons_dir()
    if _within(path, home_icons):
        return "~/.icons"
    if _within(path, xdg_icons):
        return "XDG user icons"
    if _within(path, "/usr/local/share/icons"):
        return "/usr/local/share/icons"
    if _within(path, "/usr/share/icons"):
        return "/usr/share/icons"
    return os.path.dirname(path)


def _candidate_priority(path: str) -> tuple[int, str]:
    """When the same theme ID is installed more than once, show the effective
    user override once. XDG themes are preferred unless the directory is only
    a symlink companion for a legacy ~/.icons theme (filtered separately)."""
    path = os.path.realpath(path)
    parent = os.path.dirname(path)
    if parent == _xdg_icons_dir():
        return (0, path)
    if parent == _home_icons_dir():
        return (1, path)
    if parent == os.path.realpath("/usr/local/share/icons"):
        return (2, path)
    if parent == os.path.realpath("/usr/share/icons"):
        return (3, path)
    return (4, path)


def find_theme_dirs(progress_cb=None):
    """Find one effective installed theme per theme ID.

    GNOME 51 companion directories are suppressed when their ``cursors`` link
    points back to another discovered theme. Then real shadowed copies with the
    same directory ID are collapsed to the highest-priority installation, so
    upgrading a system theme into XDG user data won't create a duplicate row.
    """
    candidates = []
    seen_paths = set()
    roots = list(_search_dirs())
    for root_index, base in enumerate(roots, 1):
        if progress_cb:
            progress_cb({
                "phase": "discovery", "root": base,
                "root_index": root_index, "roots_total": len(roots),
                "message": f"Searching icon directory {root_index}/{len(roots)}: {base}",
            })
        try:
            names = sorted(os.listdir(base), key=str.casefold)
        except OSError:
            continue
        for name_index, name in enumerate(names, 1):
            if progress_cb and (name_index == 1 or name_index % 40 == 0):
                progress_cb({
                    "phase": "discovery", "root": base,
                    "root_index": root_index, "roots_total": len(roots),
                    "entries_checked": name_index, "entries_total": len(names),
                    "message": f"Searching {base}: checking directory {name_index}/{len(names)}",
                })
            # Hidden directories are also used for atomic-swap backups and
            # staging/rollback trees. They are not installed themes and must
            # never appear as phantom entries after an interrupted process.
            if name.startswith("."):
                continue
            candidate = os.path.join(base, name)
            cursors_dir = os.path.join(candidate, "cursors")
            if not os.path.isdir(cursors_dir):
                continue
            path = os.path.realpath(candidate)
            if path in seen_paths:
                continue
            seen_paths.add(path)
            candidates.append({
                "path": path,
                "name": _theme_display_name(candidate),
                "id": os.path.basename(path),
            })

    # Drop XDG companion shells whose cursors symlink resolves to another
    # discovered theme's cursors directory.
    all_cursor_targets = {
        item["path"]: os.path.realpath(os.path.join(item["path"], "cursors"))
        for item in candidates
    }
    after_companions = []
    xdg_icons = _xdg_icons_dir()
    for item in candidates:
        path = item["path"]
        cursors_link = os.path.join(path, "cursors")
        under_xdg = os.path.dirname(path) == xdg_icons
        if under_xdg and os.path.islink(cursors_link):
            target = all_cursor_targets.get(path)
            if any(other["path"] != path and all_cursor_targets.get(other["path"]) == target
                   for other in candidates):
                continue
        after_companions.append(item)

    # A GNOME-visible user override and the system copy are one logical theme.
    # Prefer the effective user override. Do not deduplicate by display name:
    # directory IDs, not translated Name= values, drive theme inheritance.
    by_id = {}
    for item in after_companions:
        by_id.setdefault(item["id"], []).append(item)

    result = []
    for entries in by_id.values():
        winner = min(entries, key=lambda item: _candidate_priority(item["path"]))
        result.append((winner["path"], winner["name"]))
    return sorted(result, key=lambda entry: (entry[1].casefold(), os.path.basename(entry[0]).casefold()))


def inspect_theme(theme_dir, progress_cb=None):
    """Inspect every legacy cursor binary and GNOME 51 scalable companion."""
    theme_dir = os.path.realpath(os.path.expanduser(theme_dir))
    cursors_dir = os.path.join(theme_dir, "cursors")
    cursors = []
    errors = []
    max_native = 0
    try:
        entries = sorted(os.listdir(cursors_dir), key=str.casefold)
    except OSError as exc:
        entries = []
        errors.append(f"cannot read cursors/: {exc}")

    for cursor_index, name in enumerate(entries, 1):
        if progress_cb:
            progress_cb({
                "phase": "legacy_scan", "cursor": name,
                "cursor_index": cursor_index, "cursor_total": len(entries),
                "message": f"Reading legacy cursor {cursor_index}/{len(entries)}: {name}",
            })
        full = os.path.join(cursors_dir, name)
        if os.path.islink(full):
            target = os.path.basename(os.readlink(full))
            cursors.append({
                "name": name, "is_symlink": True, "target": target,
                "nominal_sizes": [], "max_size": None,
                "frame_count": 0, "animated": False,
            })
            continue
        if not os.path.isfile(full):
            continue
        try:
            with open(full, "rb") as fh:
                magic = fh.read(4)
        except OSError as exc:
            errors.append(f"{name}: {exc}")
            continue
        if magic != b"Xcur":
            continue
        try:
            frames = scan_xcursor_toc(full)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            continue

        nominal_sizes = sorted({frame["nominal_size"] for frame in frames})
        max_size = max(nominal_sizes) if nominal_sizes else 0
        max_native = max(max_native, max_size)
        counts = {}
        for frame in frames:
            size = frame["nominal_size"]
            counts[size] = counts.get(size, 0) + 1
        animated = any(count > 1 for count in counts.values())
        cursors.append({
            "name": name, "is_symlink": False, "target": None,
            "nominal_sizes": nominal_sizes, "max_size": max_size,
            "frame_count": len(frames), "animated": animated,
        })

    gnome51 = validate_scalable_theme(theme_dir, progress_cb=progress_cb)
    gnome51_writable = gnome51_module.can_write_scalable_target(theme_dir)
    parent_writable = os.access(os.path.dirname(theme_dir), os.W_OK)
    home_icons = _home_icons_dir()
    xdg_icons = _xdg_icons_dir()
    if _within(theme_dir, xdg_icons):
        install_scope = "User data"
    elif _within(theme_dir, home_icons):
        install_scope = "Home icons"
    elif _within(theme_dir, "/usr/local/share/icons"):
        install_scope = "System local"
    elif _within(theme_dir, "/usr/share/icons"):
        install_scope = "System"
    else:
        install_scope = "Other"

    return {
        "path": theme_dir,
        "id": os.path.basename(theme_dir),
        "name": _theme_display_name(theme_dir),
        "location": _location_label(theme_dir),
        "install_scope": install_scope,
        "cursors": cursors,
        "cursor_count": sum(not cursor["is_symlink"] for cursor in cursors),
        "alias_count": sum(cursor["is_symlink"] for cursor in cursors),
        "animated_count": sum(cursor["animated"] for cursor in cursors),
        "max_native_size": max_native,
        "low_res": max_native > 0 and max_native <= LOW_RES_THRESHOLD,
        # Replacement swaps the theme directory at its parent, so the parent
        # directory's permission is the relevant permission, not cursors/ mode.
        "writable": parent_writable,
        "gnome51_writable": gnome51_writable,
        "gnome51": gnome51,
        "errors": errors,
    }


def scan_all(progress_cb=None):
    """Scan every discovered theme and report incremental progress when asked.

    The callback receives small dictionaries suitable for a status UI. It is
    optional so CLI callers and existing tests can continue using scan_all().
    """
    def report(payload):
        if progress_cb:
            progress_cb(payload)

    theme_dirs = find_theme_dirs(progress_cb=progress_cb)
    total = len(theme_dirs)
    report({
        "phase": "themes_found", "themes_total": total, "themes_done": 0,
        "message": f"Found {total} cursor theme{'s' if total != 1 else ''}; preparing inspection",
    })
    results = []
    for theme_index, (theme_dir, display_name) in enumerate(theme_dirs, 1):
        name = display_name or os.path.basename(theme_dir)
        report({
            "phase": "theme_start", "theme": name, "theme_path": theme_dir,
            "theme_index": theme_index, "themes_total": total,
            "themes_done": theme_index - 1,
            "message": f"Inspecting theme {theme_index}/{total}: {name}",
        })

        def theme_progress(event, *, _name=name, _index=theme_index, _path=theme_dir):
            payload = dict(event)
            payload.setdefault("theme", _name)
            payload.setdefault("theme_path", _path)
            payload.setdefault("theme_index", _index)
            payload.setdefault("themes_total", total)
            payload.setdefault("themes_done", _index - 1)
            report(payload)

        try:
            result = inspect_theme(theme_dir, progress_cb=theme_progress)
            results.append(result)
        except Exception as exc:
            results.append({
                "path": theme_dir, "id": os.path.basename(theme_dir),
                "name": _theme_display_name(theme_dir), "location": os.path.dirname(theme_dir),
                "install_scope": "Unknown", "cursors": [], "cursor_count": 0,
                "alias_count": 0, "animated_count": 0, "max_native_size": 0,
                "low_res": False, "writable": False, "gnome51_writable": False,
                "gnome51": {"status": "invalid", "complete": False, "valid": 0,
                            "invalid": 0, "aliases": 0, "missing": [],
                            "invalid_names": [], "origin": None, "scalable_dir": None},
                "errors": [str(exc)],
            })
        report({
            "phase": "theme_done", "theme": name, "theme_path": theme_dir,
            "theme_index": theme_index, "themes_total": total,
            "themes_done": theme_index,
            "message": f"Finished theme {theme_index}/{total}: {name}",
        })
    report({
        "phase": "complete", "themes_total": total, "themes_done": total,
        "message": f"Scan complete: {total} theme{'s' if total != 1 else ''}",
    })
    return results


if __name__ == "__main__":
    import json
    print(json.dumps(scan_all(), indent=2))
