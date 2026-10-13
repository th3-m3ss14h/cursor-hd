#!/usr/bin/env python3
"""GNOME 51/KDE scalable cursor generation and validation.

The scalable cursor layout is::

    <icon-theme>/cursors_scalable/<shape>/metadata.json
    <icon-theme>/cursors_scalable/<shape>/<frame>.svg
    <icon-theme>/cursors_scalable/<alias> -> <shape>

GNOME 51 only searches XDG data icon directories and the system icon
folders for this layer. In particular, ~/.icons is an Xcursor search path,
not a GNOME 51 scalable-cursor search path. When the legacy theme is in
~/.icons, this module creates a companion theme under
$XDG_DATA_HOME/icons/<theme-name>, with cursors/index.theme symlinked back
to the legacy theme. This mirrors the layout in the supplied working
Material Bibata generator.

When no original SVGs are available, cursor frames are losslessly embedded
as PNG data inside SVG containers. This is a compatibility conversion, not
vectorization: scaling beyond the source resolution cannot create detail.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

import fsutil
import xcur_extract

SCALABLE_DIR = "cursors_scalable"
DEFAULT_DELAY_MS = 50
_ALLOWED_KEYS = {"filename", "hotspot_x", "hotspot_y", "nominal_size", "delay"}
CSS_CURSOR_NAMES = [
    "default", "context-menu", "help", "pointer", "progress", "wait",
    "cell", "crosshair", "text", "vertical-text", "alias", "copy",
    "move", "no-drop", "not-allowed", "grab", "grabbing",
    "e-resize", "n-resize", "ne-resize", "nw-resize", "s-resize",
    "se-resize", "sw-resize", "w-resize", "ew-resize", "ns-resize",
    "nesw-resize", "nwse-resize", "col-resize", "row-resize",
    "all-scroll", "zoom-in", "zoom-out", "dnd-ask", "all-resize",
]
# Mapping from GNOME/Mutter's CSS cursor names to their legacy X11 fallback.
# Keep this aligned with the known-working Material Bibata generator.
CSS_TO_LEGACY = {
    "default": "left_ptr", "context-menu": "left_ptr", "help": "question_arrow",
    "pointer": "hand", "progress": "left_ptr_watch", "wait": "watch",
    "cell": "crosshair", "crosshair": "cross", "text": "xterm",
    "vertical-text": "xterm", "alias": "dnd-link", "copy": "dnd-copy",
    "move": "dnd-move", "no-drop": "dnd-none", "not-allowed": "crossed_circle",
    "grab": "hand2", "grabbing": "hand2", "e-resize": "right_side",
    "n-resize": "top_side", "ne-resize": "top_right_corner",
    "nw-resize": "top_left_corner", "s-resize": "bottom_side",
    "se-resize": "bottom_right_corner", "sw-resize": "bottom_left_corner",
    "w-resize": "left_side", "ew-resize": "h_double_arrow",
    "ns-resize": "v_double_arrow", "nesw-resize": "fd_double_arrow",
    "nwse-resize": "bd_double_arrow", "col-resize": "h_double_arrow",
    "row-resize": "v_double_arrow", "all-scroll": "left_ptr",
    "zoom-in": "left_ptr", "zoom-out": "left_ptr", "dnd-ask": "dnd-copy",
    "all-resize": "dnd-move",
}
# Alternate names commonly found in Xcursor themes. The exact CSS shape name
# always wins, followed by CSS_TO_LEGACY, then these compatibility aliases.
CSS_ALTERNATES = {
    "default": ("arrow",),
    "context-menu": ("left_ptr", "arrow"),
    "help": ("help", "whats_this"),
    "pointer": ("left_ptr", "default", "arrow", "hand1"),
    "progress": ("watch", "wait"),
    "wait": ("left_ptr_watch",),
    "cell": ("plus",),
    "crosshair": ("crosshair",),
    "vertical-text": ("vertical-text", "v_double_arrow"),
    "copy": ("copy",),
    "alias": ("alias",),
    "move": ("all-scroll",),
    "no-drop": ("dnd-no-drop", "dnd_no_drop", "no-drop"),
    "not-allowed": ("no-drop", "dnd-no-drop", "dnd_no_drop"),
    "grab": ("openhand", "hand1"),
    "grabbing": ("closedhand", "hand1"),
    "ew-resize": ("sb_h_double_arrow", "h_double_arrow"),
    "ns-resize": ("sb_v_double_arrow", "v_double_arrow"),
    "nesw-resize": ("nesw_double_arrow",),
    "nwse-resize": ("nwse_double_arrow",),
    "all-scroll": ("move",),
    "dnd-ask": ("dnd-link",),
    "all-resize": ("grabbing",),
}


def _xdg_icons_dir() -> str:
    data_home = os.path.expanduser(os.environ.get("XDG_DATA_HOME", "~/.local/share"))
    return os.path.realpath(os.path.join(data_home, "icons"))


def _is_within(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath((os.path.realpath(path), os.path.realpath(parent))) == os.path.realpath(parent)
    except (ValueError, OSError):
        return False


def is_xdg_theme_dir(theme_dir: str) -> bool:
    """Whether theme_dir is directly under the user's XDG icon theme tree."""
    path = os.path.realpath(os.path.expanduser(theme_dir))
    icons = _xdg_icons_dir()
    return os.path.dirname(path) == icons


def scalable_theme_target(theme_dir: str) -> str:
    """Return the theme directory where GNOME 51 scalable data belongs."""
    theme_dir = os.path.realpath(os.path.expanduser(theme_dir))
    if is_xdg_theme_dir(theme_dir):
        return theme_dir
    return os.path.join(_xdg_icons_dir(), os.path.basename(theme_dir))


def _is_companion_for(companion_dir: str, legacy_theme_dir: str) -> bool:
    """True only for a companion whose cursors symlink points to this theme."""
    companion_dir = os.path.abspath(companion_dir)
    legacy_theme_dir = os.path.realpath(legacy_theme_dir)
    link = os.path.join(companion_dir, "cursors")
    expected = os.path.realpath(os.path.join(legacy_theme_dir, "cursors"))
    return os.path.islink(link) and os.path.realpath(link) == expected


def is_companion_for(companion_dir: str, legacy_theme_dir: str) -> bool:
    """Public companion check used by the scanner and UI operations."""
    return _is_companion_for(companion_dir, legacy_theme_dir)


def can_write_scalable_target(theme_dir: str) -> bool:
    """Check the directory this operation will write, not the legacy source."""
    source = os.path.realpath(os.path.expanduser(theme_dir))
    target = scalable_theme_target(source)
    if os.path.realpath(target) == source:
        return os.path.isdir(source) and os.access(source, os.W_OK)
    if os.path.lexists(target):
        return _is_companion_for(target, source) and os.access(target, os.W_OK)
    parent = os.path.dirname(target)
    while not os.path.exists(parent):
        next_parent = os.path.dirname(parent)
        if next_parent == parent:
            break
        parent = next_parent
    return os.path.isdir(parent) and os.access(parent, os.W_OK)


def _system_theme_dir(theme_dir: str) -> bool:
    path = os.path.realpath(theme_dir)
    return any(_is_within(path, p) for p in ("/usr/share/icons", "/usr/local/share/icons"))


def existing_scalable_theme_dir(theme_dir: str) -> str | None:
    """Find the scalable theme tree that is actually associated with theme_dir.

    ~/.icons/cursors_scalable is deliberately ignored: GNOME does not search
    ~/.icons for this format. System icon trees remain valid fallbacks.
    """
    theme_dir = os.path.realpath(os.path.expanduser(theme_dir))
    if is_xdg_theme_dir(theme_dir):
        return theme_dir if os.path.isdir(os.path.join(theme_dir, SCALABLE_DIR)) else None

    companion = scalable_theme_target(theme_dir)
    if os.path.isdir(companion) and _is_companion_for(companion, theme_dir):
        if os.path.isdir(os.path.join(companion, SCALABLE_DIR)):
            return companion
    if _system_theme_dir(theme_dir) and os.path.isdir(os.path.join(theme_dir, SCALABLE_DIR)):
        return theme_dir
    # Custom install roots (outside ~/.icons and system paths) may be used by
    # callers/tests and by users with nonstandard Xcursor search paths.
    icons_home = os.path.realpath(os.path.expanduser("~/.icons"))
    if not _is_within(theme_dir, icons_home) and os.path.isdir(os.path.join(theme_dir, SCALABLE_DIR)):
        return theme_dir
    return None


def scalable_origin(theme_dir: str, progress_cb=None) -> str | None:
    """Classify existing scalable data as vector, raster-wrapped, or unknown."""
    scalable_theme = existing_scalable_theme_dir(theme_dir)
    if not scalable_theme:
        return None
    root = os.path.join(scalable_theme, SCALABLE_DIR)
    saw_raster = False
    names = sorted(os.listdir(root))
    for index, name in enumerate(names, 1):
        if progress_cb:
            progress_cb({
                "phase": "scalable_artwork", "cursor": name,
                "cursor_index": index, "cursor_total": len(names),
                "message": f"Checking scalable artwork {index}/{len(names)}: {name}",
            })
        path = os.path.join(root, name)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        for fn in sorted(os.listdir(path)):
            if not fn.lower().endswith(".svg"):
                continue
            try:
                with open(os.path.join(path, fn), "rb") as f:
                    data = f.read(262144)
            except OSError:
                continue
            # The converter-generated wrapper has only an <image> element.
            if b"<image" in data and not any(tag in data for tag in (
                b"<path", b"<rect", b"<circle", b"<ellipse", b"<polygon",
                b"<polyline", b"<line", b"<g ", b"<g>", b"<use", b"<text",
            )):
                saw_raster = True
            else:
                return "vector"
            break
    return "raster" if saw_raster else "unknown"


def _is_xcursor(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"Xcur"
    except OSError:
        return False


def _resolve_terminal(cursors_dir: str, name: str, max_depth: int = 32) -> str | None:
    """Resolve a legacy Xcursor symlink to a regular file inside cursors_dir."""
    root = os.path.realpath(cursors_dir)
    current = name
    seen = set()
    for _ in range(max_depth):
        if not isinstance(current, str) or current in ("", ".", "..") or os.path.basename(current) != current:
            return None
        path = os.path.join(cursors_dir, current)
        if current in seen:
            return None
        seen.add(current)
        if not os.path.islink(path):
            if not os.path.isfile(path):
                return None
            return current if os.path.commonpath((root, os.path.realpath(path))) == root else None
        target = os.readlink(path)
        if os.path.isabs(target):
            if os.path.realpath(os.path.dirname(target)) != root:
                return None
            current = os.path.basename(target)
        else:
            norm = os.path.normpath(target)
            if os.sep in norm or norm in (".", ".."):
                return None
            current = norm
    return None


def _safe_component(name: str) -> bool:
    return (isinstance(name, str) and name not in ("", ".", "..")
            and os.path.basename(name) == name
            and not any(ord(c) < 32 or ord(c) == 127 for c in name))


def _frame_svg(frame: dict) -> str:
    """Embed a decoded Xcursor frame losslessly in an SVG container."""
    w, h = int(frame["width"]), int(frame["height"])
    rgba = xcur_extract.bgra_premult_to_rgba(frame["pixels_bgra_premult"], w, h)
    buf = io.BytesIO()
    Image.frombytes("RGBA", (w, h), rgba).save(buf, format="PNG")
    uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
        f'<image width="{w}" height="{h}" preserveAspectRatio="none" '
        f'xlink:href="{uri}" href="{uri}"/></svg>\n'
    )


def _convert_cursor(src_path: str, dst_dir: str) -> tuple[int, int, list[str]]:
    frames = xcur_extract.parse_xcursor(src_path)
    if not frames:
        raise ValueError("no image frames")
    nominal = max(int(f["nominal_size"]) for f in frames)
    group = [f for f in frames if int(f["nominal_size"]) == nominal]
    animated = len(group) > 1
    warnings = []
    os.makedirs(dst_dir)
    entries = []
    for i, frame in enumerate(group):
        filename = f"frame-{i:04d}.svg"
        with open(os.path.join(dst_dir, filename), "w", encoding="utf-8") as f:
            f.write(_frame_svg(frame))
        entry = {
            "filename": filename,
            "hotspot_x": int(frame["xhot"]),
            "hotspot_y": int(frame["yhot"]),
            "nominal_size": nominal,
        }
        if animated:
            delay = int(frame["delay"])
            if delay <= 0:
                delay = DEFAULT_DELAY_MS
                warning = f"zero delay in animated cursor, using {DEFAULT_DELAY_MS}ms"
                if warning not in warnings:
                    warnings.append(warning)
            entry["delay"] = delay
        if (int(frame["width"]), int(frame["height"])) != (nominal, nominal):
            warning = f"bitmap {frame['width']}x{frame['height']} differs from nominal size {nominal}"
            if warning not in warnings:
                warnings.append(warning)
        entries.append(entry)
    with open(os.path.join(dst_dir, "metadata.json"), "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
        f.write("\n")
    return nominal, len(group), warnings


def _resolve_scalable_terminal(root: str, name: str, max_depth: int = 32) -> str | None:
    root_real = os.path.realpath(root)
    current = name
    seen = set()
    for _ in range(max_depth):
        if not _safe_component(current) or current in seen:
            return None
        seen.add(current)
        path = os.path.join(root, current)
        if not os.path.islink(path):
            if os.path.isdir(path) and _check_cursor_dir(path) is None:
                return current if os.path.commonpath((root_real, os.path.realpath(path))) == root_real else None
            return None
        target = os.readlink(path)
        if os.path.isabs(target):
            if os.path.realpath(os.path.dirname(target)) != root_real:
                return None
            current = os.path.basename(target)
        else:
            normalized = os.path.normpath(target)
            if normalized in ("", ".", "..") or os.sep in normalized:
                return None
            current = normalized
    return None


def _copy_existing_scalable(src_dir: str | None, dest_dir: str) -> tuple[set[str], set[str]]:
    if not src_dir or not os.path.isdir(src_dir):
        return set(), set()
    preserved: set[str] = set()
    aliases: set[str] = set()
    root = os.path.realpath(src_dir)
    for name in sorted(os.listdir(src_dir)):
        if not _safe_component(name):
            continue
        src = os.path.join(src_dir, name)
        dst = os.path.join(dest_dir, name)
        if os.path.islink(src):
            terminal = _resolve_scalable_terminal(root, name)
            if terminal is None or os.path.lexists(dst):
                continue
            try:
                os.symlink(terminal, dst)
                aliases.add(name)
            except OSError:
                continue
        elif os.path.isdir(src):
            try:
                shutil.copytree(src, dst, symlinks=True)
                reason = _check_cursor_dir(dst)
                if reason is None:
                    preserved.add(name)
                else:
                    shutil.rmtree(dst, ignore_errors=True)
            except OSError:
                shutil.rmtree(dst, ignore_errors=True)
    return preserved, aliases


def _target_for_css_name(dest_dir: str, candidates: tuple[str, ...], available: set[str]) -> str | None:
    for candidate in candidates:
        if candidate not in available:
            continue
        terminal = _resolve_scalable_terminal(dest_dir, candidate)
        if terminal is not None:
            return terminal
    return None


def _add_css_aliases(dest_dir: str, available: set[str], aliases: set[str]) -> set[str]:
    """Add the CSS/freedesktop aliases GNOME 51 asks Mutter for."""
    created = set()
    for css_name in CSS_CURSOR_NAMES:
        dest = os.path.join(dest_dir, css_name)
        if os.path.lexists(dest):
            # Existing valid native or alias shape wins. Invalid copied aliases
            # are normally filtered earlier, but handle them defensively.
            if os.path.islink(dest) and _resolve_scalable_terminal(dest_dir, css_name) is None:
                os.unlink(dest)
            else:
                continue
        candidates = (css_name, CSS_TO_LEGACY.get(css_name, "")) + CSS_ALTERNATES.get(css_name, ())
        target = _target_for_css_name(dest_dir, candidates, available | aliases | created)
        if target and target != css_name:
            try:
                os.symlink(target, dest)
                created.add(css_name)
            except FileExistsError:
                pass
    return created


def build_scalable_dir(theme_dir: str, dest_dir: str, progress_cb=None,
                       preserve_dir: str | None = None) -> dict:
    """Build a fresh scalable directory from the theme's legacy Xcursor files.

    Valid existing vector shapes and aliases in preserve_dir are kept. Missing
    shapes are generated from Xcursor frames; each cursor uses its largest
    nominal-size animation group, because svg-cursor metadata represents one
    scalable size rather than a multi-size Xcursor ladder.
    """
    cursors_dir = os.path.join(theme_dir, "cursors")
    if not os.path.isdir(cursors_dir):
        raise ValueError(f"{theme_dir} has no cursors/ directory")
    os.makedirs(dest_dir)
    generated: list[str] = []
    aliases: set[str] = set()
    skipped: list[dict] = []
    warnings: list[str] = []
    sizes: dict[str, int] = {}
    converted: set[str] = set()
    preserved: set[str] = set()

    if preserve_dir:
        preserved, preserved_aliases = _copy_existing_scalable(preserve_dir, dest_dir)
        aliases.update(preserved_aliases)

    names = sorted(os.listdir(cursors_dir))
    for name in names:
        path = os.path.join(cursors_dir, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        if name in preserved or not _safe_component(name):
            if not _safe_component(name):
                skipped.append({"cursor": name, "reason": "unsafe file name"})
            continue
        if not _is_xcursor(path):
            skipped.append({"cursor": name, "reason": "not an Xcursor file"})
            continue
        try:
            nominal, _nframes, cursor_warnings = _convert_cursor(path, os.path.join(dest_dir, name))
        except Exception as exc:
            skipped.append({"cursor": name, "reason": str(exc)})
            if progress_cb:
                progress_cb({"cursor": name, "status": "error", "error": str(exc)})
            continue
        converted.add(name)
        generated.append(name)
        sizes[name] = nominal
        warnings.extend(f"{name}: {warning}" for warning in cursor_warnings)
        if progress_cb:
            progress_cb({"cursor": name, "status": "ok"})

    for name in names:
        path = os.path.join(cursors_dir, name)
        if not os.path.islink(path) or not _safe_component(name):
            continue
        if os.path.lexists(os.path.join(dest_dir, name)):
            continue
        terminal = _resolve_terminal(cursors_dir, name)
        if terminal is None or (terminal not in converted and terminal not in preserved):
            skipped.append({"cursor": name, "reason": "alias does not resolve to a converted or preserved cursor"})
            continue
        try:
            os.symlink(terminal, os.path.join(dest_dir, name))
            aliases.add(name)
        except OSError as exc:
            skipped.append({"cursor": name, "reason": f"could not create alias: {exc}"})

    available = set(converted) | set(preserved)
    aliases.update(_add_css_aliases(dest_dir, available, aliases))
    aliases = set(aliases) | {
        name for name in CSS_CURSOR_NAMES
        if os.path.islink(os.path.join(dest_dir, name))
        and _resolve_scalable_terminal(dest_dir, name) is not None
    }

    if not generated and not preserved:
        details = "; ".join(f"{item['cursor']}: {item['reason']}" for item in skipped[:5])
        raise RuntimeError("no cursors could be converted" + (f": {details}" if details else ""))
    return {
        "generated": generated,
        "aliases": sorted(aliases),
        "skipped": skipped,
        "warnings": warnings,
        "sizes": sizes,
    }


def _minimal_index_theme(dst_path: str, theme_name: str) -> None:
    content = (
        "[Icon Theme]\n"
        f"Name={theme_name}\n"
        "Comment=Cursor theme with GNOME 51 scalable cursors\n"
        "Directories=cursors_scalable\n"
        "\n[cursors_scalable]\n"
        "Size=24\nType=Scalable\nContext=Cursors\n"
    )
    with open(dst_path, "w", encoding="utf-8") as f:
        f.write(content)


def _ensure_companion_shell(stage_theme_dir: str, target_theme_dir: str,
                            legacy_theme_dir: str, theme_name: str) -> None:
    """Create the XDG-visible theme shell and point it at legacy cursor data."""
    os.makedirs(stage_theme_dir, exist_ok=True)
    os.symlink(os.path.join(os.path.realpath(legacy_theme_dir), "cursors"),
               os.path.join(stage_theme_dir, "cursors"))
    legacy_index = os.path.join(os.path.realpath(legacy_theme_dir), "index.theme")
    if os.path.isfile(legacy_index):
        os.symlink(legacy_index, os.path.join(stage_theme_dir, "index.theme"))
    else:
        _minimal_index_theme(os.path.join(stage_theme_dir, "index.theme"), theme_name)


def _check_companion_target(target: str, legacy_theme_dir: str) -> None:
    if not os.path.lexists(target):
        return
    if not os.path.isdir(target):
        raise ValueError(f"GNOME 51 output path exists but is not a directory: {target}")
    if not _is_companion_for(target, legacy_theme_dir):
        raise ValueError(
            f"{target} already exists and is not a GNOME 51 companion for {legacy_theme_dir}. "
            "Refusing to replace another icon theme with the same directory name."
        )


def _choose_preserve_dir(theme_dir: str, target_theme_dir: str,
                         explicit_preserve_dir: str | None = None) -> str | None:
    if explicit_preserve_dir and os.path.isdir(explicit_preserve_dir):
        return explicit_preserve_dir
    target_scalable = os.path.join(target_theme_dir, SCALABLE_DIR)
    if os.path.isdir(target_scalable) and scalable_origin_from_root(target_scalable) == "vector":
        return target_scalable
    source_effective = existing_scalable_theme_dir(theme_dir)
    if source_effective:
        source_scalable = os.path.join(source_effective, SCALABLE_DIR)
        if os.path.isdir(source_scalable) and scalable_origin_from_root(source_scalable) == "vector":
            return source_scalable
    return None


def scalable_origin_from_root(root: str, progress_cb=None) -> str:
    if not os.path.isdir(root):
        return "unknown"
    raster = False
    scalable_names = sorted(os.listdir(root))
    for index, name in enumerate(scalable_names, 1):
        if progress_cb:
            progress_cb({
                "phase": "scalable_validation", "cursor": name,
                "cursor_index": index, "cursor_total": len(scalable_names),
                "message": f"Validating scalable cursor {index}/{len(scalable_names)}: {name}",
            })
        path = os.path.join(root, name)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        for filename in sorted(os.listdir(path)):
            if not filename.lower().endswith(".svg"):
                continue
            try:
                with open(os.path.join(path, filename), "rb") as f:
                    data = f.read(262144)
            except OSError:
                continue
            if b"<image" in data and not any(tag in data for tag in (
                b"<path", b"<rect", b"<circle", b"<ellipse", b"<polygon",
                b"<polyline", b"<line", b"<g ", b"<g>", b"<use", b"<text",
            )):
                raster = True
            else:
                return "vector"
            break
    return "raster" if raster else "unknown"


def _build_scalable_stage(theme_dir: str, stage_dir: str, target_theme_dir: str,
                          progress_cb=None, preserve_dir: str | None = None) -> dict:
    return build_scalable_dir(
        theme_dir, stage_dir, progress_cb=progress_cb,
        preserve_dir=_choose_preserve_dir(theme_dir, target_theme_dir, preserve_dir),
    )


def generate_in_place(theme_dir: str, progress_cb=None,
                      preserve_dir: str | None = None) -> dict:
    """Add/refresh GNOME 51 support without modifying legacy cursor files.

    For ~/.icons themes, this writes to the XDG companion theme instead of the
    ineffective ~/.icons/<theme>/cursors_scalable path.
    """
    theme_dir = os.path.realpath(os.path.expanduser(theme_dir))
    if not os.path.isdir(os.path.join(theme_dir, "cursors")):
        raise ValueError(f"{theme_dir} has no cursors/ directory")

    target_theme_dir = scalable_theme_target(theme_dir)
    source_is_target = os.path.realpath(target_theme_dir) == os.path.realpath(theme_dir)
    if not source_is_target:
        _check_companion_target(target_theme_dir, theme_dir)

    if source_is_target:
        os.makedirs(theme_dir, exist_ok=True)
        old_scalable = os.path.join(theme_dir, SCALABLE_DIR)
        stage_root = tempfile.mkdtemp(prefix=".cursors_scalable.stage-", dir=theme_dir)
        try:
            stage = os.path.join(stage_root, SCALABLE_DIR)
            result = _build_scalable_stage(theme_dir, stage, target_theme_dir, progress_cb, preserve_dir)
            fsutil.commit_dir(stage, old_scalable)
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)
    elif os.path.isdir(target_theme_dir):
        # Existing companion: update only its scalable layer; leave cursor and
        # index symlinks untouched.
        cursors_link = os.path.join(target_theme_dir, "cursors")
        if not os.path.islink(cursors_link) or os.path.realpath(cursors_link) != os.path.realpath(os.path.join(theme_dir, "cursors")):
            raise ValueError(f"invalid GNOME 51 companion cursors link in {target_theme_dir}")
        legacy_index = os.path.join(theme_dir, "index.theme")
        target_index = os.path.join(target_theme_dir, "index.theme")
        if os.path.isfile(legacy_index) and not os.path.lexists(target_index):
            os.symlink(os.path.realpath(legacy_index), target_index)
        stage_root = tempfile.mkdtemp(prefix=".cursors_scalable.stage-", dir=target_theme_dir)
        try:
            stage = os.path.join(stage_root, SCALABLE_DIR)
            result = _build_scalable_stage(theme_dir, stage, target_theme_dir, progress_cb, preserve_dir)
            fsutil.commit_dir(stage, os.path.join(target_theme_dir, SCALABLE_DIR))
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)
    else:
        parent = os.path.dirname(target_theme_dir)
        os.makedirs(parent, exist_ok=True)
        stage_root = tempfile.mkdtemp(prefix=f".{os.path.basename(target_theme_dir)}.gnome51-", dir=parent)
        try:
            stage_theme = os.path.join(stage_root, os.path.basename(target_theme_dir))
            _ensure_companion_shell(stage_theme, target_theme_dir, theme_dir, os.path.basename(theme_dir))
            stage_scalable = os.path.join(stage_theme, SCALABLE_DIR)
            result = _build_scalable_stage(theme_dir, stage_scalable, target_theme_dir, progress_cb, preserve_dir)
            fsutil.commit_dir(stage_theme, target_theme_dir)
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)

    result["out_dir"] = theme_dir
    result["scalable_dir"] = os.path.join(target_theme_dir, SCALABLE_DIR)
    result["processed"] = result["generated"]
    result["errors"] = []
    return result


def _set_index_name(theme_dir: str, name: str) -> None:
    path = os.path.join(theme_dir, "index.theme")
    lines = []
    if os.path.islink(path):
        # Never open a copied index.theme symlink for writing: that would
        # silently rename the source theme's index file.
        linked_target = os.path.realpath(path)
        if os.path.isfile(linked_target):
            with open(linked_target, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        os.unlink(path)
    elif os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    in_icon = False
    replaced = False
    header_at = None
    out = []
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_icon = stripped.lower() == "[icon theme]"
            if in_icon and header_at is None:
                header_at = len(out)
            out.append(raw)
            continue
        if in_icon and "=" in stripped and stripped.split("=", 1)[0].strip().lower() == "name":
            if not replaced:
                out.append(f"Name={name}")
                replaced = True
            continue
        out.append(raw)
    if not replaced:
        if header_at is None:
            out.insert(0, "[Icon Theme]")
            header_at = 0
        out.insert(header_at + 1, f"Name={name}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip("\n") + "\n")


def generate_theme(theme_dir: str, out_dir: str, theme_name: str | None = None,
                   progress_cb=None, preserve_dir: str | None = None) -> dict:
    """Create a separate Xcursor copy plus the GNOME 51 XDG companion layer."""
    theme_dir = os.path.realpath(os.path.expanduser(theme_dir))
    out_dir = os.path.abspath(os.path.expanduser(out_dir))
    if os.path.realpath(out_dir) == theme_dir:
        raise ValueError("use generate_in_place() to augment the source theme")
    if not os.path.isdir(os.path.join(theme_dir, "cursors")):
        raise ValueError(f"{theme_dir} has no cursors/ directory")
    if os.path.commonpath((theme_dir, os.path.realpath(out_dir))) == theme_dir:
        raise ValueError("output theme cannot be placed inside the source theme")
    display_name = theme_name or os.path.basename(out_dir)

    target_theme_dir = scalable_theme_target(out_dir)
    companion_needed = os.path.realpath(target_theme_dir) != os.path.realpath(out_dir)
    if companion_needed:
        _check_companion_target(target_theme_dir, out_dir)

    parent = os.path.dirname(out_dir)
    os.makedirs(parent, exist_ok=True)
    stage_root = tempfile.mkdtemp(prefix=f".{os.path.basename(out_dir)}.gnome51-", dir=parent)
    stage_theme = os.path.join(stage_root, os.path.basename(out_dir))
    companion_stage_root = None
    try:
        shutil.copytree(theme_dir, stage_theme, symlinks=True)
        _set_index_name(stage_theme, display_name)
        cursor_theme_path = os.path.join(stage_theme, "cursor.theme")
        if os.path.lexists(cursor_theme_path):
            fsutil.set_icon_theme_name(cursor_theme_path, display_name)
        source_scalable = existing_scalable_theme_dir(theme_dir)
        vector_preserve = preserve_dir
        if vector_preserve is None and source_scalable and scalable_origin_from_root(os.path.join(source_scalable, SCALABLE_DIR)) == "vector":
            vector_preserve = os.path.join(source_scalable, SCALABLE_DIR)

        if not companion_needed:
            # This output is itself in XDG_DATA_HOME/icons, so its own scaler
            # is the correct one. Existing copied legacy scalable data is only
            # preserved when it is true vector artwork.
            stale = os.path.join(stage_theme, SCALABLE_DIR)
            if os.path.lexists(stale):
                if os.path.islink(stale) or not os.path.isdir(stale):
                    os.unlink(stale)
                else:
                    shutil.rmtree(stale)
            result = build_scalable_dir(theme_dir, os.path.join(stage_theme, SCALABLE_DIR),
                                        progress_cb=progress_cb, preserve_dir=vector_preserve)
            fsutil.commit_dir(stage_theme, out_dir)
            scalable_out = os.path.join(out_dir, SCALABLE_DIR)
        else:
            # The ~/.icons copy is the legacy/Xcursor theme. Its scalable
            # implementation lives in the sibling XDG companion directory.
            stale = os.path.join(stage_theme, SCALABLE_DIR)
            if os.path.lexists(stale):
                if os.path.islink(stale) or not os.path.isdir(stale):
                    os.unlink(stale)
                else:
                    shutil.rmtree(stale)

            xdg_parent = os.path.dirname(target_theme_dir)
            os.makedirs(xdg_parent, exist_ok=True)
            companion_stage_root = tempfile.mkdtemp(prefix=f".{os.path.basename(target_theme_dir)}.gnome51-", dir=xdg_parent)
            if os.path.isdir(target_theme_dir):
                # Existing companion is known to point to the requested output.
                stage_companion = os.path.join(companion_stage_root, os.path.basename(target_theme_dir))
                shutil.copytree(target_theme_dir, stage_companion, symlinks=True)
                existing_scale = os.path.join(stage_companion, SCALABLE_DIR)
                if os.path.lexists(existing_scale):
                    if os.path.islink(existing_scale) or not os.path.isdir(existing_scale):
                        os.unlink(existing_scale)
                    else:
                        shutil.rmtree(existing_scale)
            else:
                stage_companion = os.path.join(companion_stage_root, os.path.basename(target_theme_dir))
                os.makedirs(stage_companion)
                os.symlink(os.path.join(out_dir, "cursors"), os.path.join(stage_companion, "cursors"))
                os.symlink(os.path.join(out_dir, "index.theme"), os.path.join(stage_companion, "index.theme"))
            result = build_scalable_dir(theme_dir, os.path.join(stage_companion, SCALABLE_DIR),
                                        progress_cb=progress_cb, preserve_dir=vector_preserve)
            # Replace legacy copy first, then the companion. If the second swap
            # fails, leave the useful legacy copy in place and report the error.
            fsutil.commit_dir(stage_theme, out_dir)
            fsutil.commit_dir(stage_companion, target_theme_dir)
            scalable_out = os.path.join(target_theme_dir, SCALABLE_DIR)

        result["out_dir"] = out_dir
        result["scalable_dir"] = scalable_out
        result["processed"] = result["generated"]
        result["errors"] = []
        return result
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)
        if companion_stage_root:
            shutil.rmtree(companion_stage_root, ignore_errors=True)


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def check_metadata(entries) -> str | None:
    if not isinstance(entries, list) or not entries:
        return "metadata.json is not a non-empty array"
    for entry in entries:
        if not isinstance(entry, dict):
            return "entry is not an object"
        extra = set(entry) - _ALLOWED_KEYS
        if extra:
            return "unknown key(s): " + ", ".join(sorted(extra))
        filename = entry.get("filename")
        if (not isinstance(filename, str) or not filename or os.path.basename(filename) != filename
                or not filename.lower().endswith(".svg") or not _safe_component(filename)):
            return "bad or missing SVG filename"
        if not _is_num(entry.get("hotspot_x")) or not _is_num(entry.get("hotspot_y")):
            return "hotspot_x/hotspot_y must be numbers"
        if not _is_num(entry.get("nominal_size")) or entry["nominal_size"] <= 0:
            return "nominal_size must be a number > 0"
        if len(entries) > 1 and (not _is_num(entry.get("delay")) or entry["delay"] <= 0):
            return "animated cursor: every entry needs delay > 0"
    if len(entries) > 1 and len({entry["nominal_size"] for entry in entries}) > 1:
        return "frames have differing nominal sizes (format has one size per cursor)"
    return None


def _check_cursor_dir(path: str) -> str | None:
    metadata_path = os.path.join(path, "metadata.json")
    if not os.path.isfile(metadata_path):
        return "missing metadata.json"
    try:
        with open(metadata_path, encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, ValueError) as exc:
        return f"unreadable metadata.json: {exc}"
    reason = check_metadata(entries)
    if reason:
        return reason
    for entry in entries:
        svg_path = os.path.join(path, entry["filename"])
        if not os.path.isfile(svg_path):
            return f"missing SVG {entry['filename']!r}"
        try:
            if os.path.getsize(svg_path) > 64 * 1024 * 1024:
                return f"SVG {entry['filename']!r} exceeds 64 MiB"
            root = ET.parse(svg_path).getroot()
        except (OSError, ET.ParseError) as exc:
            return f"invalid SVG {entry['filename']!r}: {exc}"
        if root.tag.rsplit("}", 1)[-1].lower() != "svg":
            return f"{entry['filename']!r} does not have an SVG root"
    return None


def _legacy_cursor_names(theme_dir: str, progress_cb=None) -> list[str]:
    cursors_dir = os.path.join(theme_dir, "cursors")
    names = []
    if not os.path.isdir(cursors_dir):
        return names
    entries = sorted(os.listdir(cursors_dir))
    for index, name in enumerate(entries, 1):
        if progress_cb:
            progress_cb({
                "phase": "legacy_coverage", "cursor": name,
                "cursor_index": index, "cursor_total": len(entries),
                "message": f"Checking legacy coverage {index}/{len(entries)}: {name}",
            })
        terminal = _resolve_terminal(cursors_dir, name)
        if terminal and _is_xcursor(os.path.join(cursors_dir, terminal)):
            names.append(name)
    return names


def validate_scalable_theme(theme_dir: str, progress_cb=None) -> dict:
    """Validate the GNOME 51 layer, including legacy and CSS cursor coverage."""
    scalable_theme = existing_scalable_theme_dir(theme_dir)
    if not scalable_theme:
        return {"status": "missing", "complete": False, "valid": 0, "invalid": 0,
                "aliases": 0, "missing": [], "invalid_names": [], "origin": None,
                "scalable_dir": None}
    root = os.path.join(scalable_theme, SCALABLE_DIR)
    valid = 0
    aliases = 0
    invalid_names = []
    covered = set()
    scalable_names = sorted(os.listdir(root))
    for index, name in enumerate(scalable_names, 1):
        if progress_cb:
            progress_cb({
                "phase": "scalable_validation", "cursor": name,
                "cursor_index": index, "cursor_total": len(scalable_names),
                "message": f"Validating scalable cursor {index}/{len(scalable_names)}: {name}",
            })
        path = os.path.join(root, name)
        if os.path.islink(path):
            terminal = _resolve_scalable_terminal(root, name)
            if terminal is not None:
                aliases += 1
                covered.add(name)
            else:
                invalid_names.append({"name": name, "reason": "alias does not resolve to a valid cursor"})
            continue
        if not os.path.isdir(path):
            continue
        reason = _check_cursor_dir(path)
        if reason:
            invalid_names.append({"name": name, "reason": reason})
        else:
            valid += 1
            covered.add(name)

    missing_legacy = [name for name in _legacy_cursor_names(theme_dir, progress_cb=progress_cb) if name not in covered]
    missing_css = [name for name in CSS_CURSOR_NAMES if name not in covered]
    missing = sorted(set(missing_legacy) | set(missing_css))
    complete = valid > 0 and not invalid_names and not missing
    if complete:
        status = "compatible"
    elif valid or aliases:
        status = "partial"
    else:
        status = "invalid"
    return {
        "status": status, "complete": complete, "valid": valid,
        "invalid": len(invalid_names), "aliases": aliases,
        "missing": missing, "invalid_names": invalid_names[:30],
        "origin": scalable_origin(theme_dir, progress_cb=progress_cb),
        "scalable_dir": root,
    }


def _cli() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Generate GNOME 51 scalable cursors for an Xcursor theme")
    ap.add_argument("theme_dir")
    ap.add_argument("--out", help="write a separate legacy theme copy plus XDG scalable companion")
    ap.add_argument("--name", help="display name for the separate copy")
    ap.add_argument("--validate", action="store_true", help="only validate, do not generate")
    args = ap.parse_args()
    if args.validate:
        print(json.dumps(validate_scalable_theme(args.theme_dir), indent=2))
    elif args.out:
        result = generate_theme(args.theme_dir, args.out, theme_name=args.name,
                                progress_cb=lambda s: print(json.dumps(s)))
        print(json.dumps({k: result[k] for k in ("out_dir", "scalable_dir", "generated", "aliases", "skipped", "warnings")}, indent=2))
    else:
        result = generate_in_place(args.theme_dir, progress_cb=lambda s: print(json.dumps(s)))
        print(json.dumps({k: result[k] for k in ("out_dir", "scalable_dir", "generated", "aliases", "skipped", "warnings")}, indent=2))


if __name__ == "__main__":
    _cli()
