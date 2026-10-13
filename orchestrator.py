#!/usr/bin/env python3
"""
Orchestrate extract -> upscale -> rebuild for one Xcursor theme.

The conversion is staged outside the destination and committed only after
all selected cursors have succeeded. This prevents a failed conversion from
leaving a half-built theme behind, including in replace-in-place mode.
"""
import os
import shutil
import tempfile
import uuid

from xcur_extract import dump_cursor
from xcur_upscale import upscale_manifest
from xcur_rebuild import rebuild_cursor
import fsutil
import gnome51
from scanner import inspect_theme


MAX_ALIAS_DEPTH = 64


def _space_free_temp_parent():
    """Choose a temporary parent whose path is safe in xcursorgen's config.

    xcursorgen parses its config as whitespace-delimited fields and does not
    consistently support quoted paths. Keep generated PNG paths free of
    whitespace even when TMPDIR is configured to a directory such as
    ``/home/user/My Temp``.
    """
    for candidate in ("/tmp", "/var/tmp", tempfile.gettempdir()):
        if os.path.isdir(candidate) and not any(ch.isspace() for ch in os.path.abspath(candidate)):
            return candidate
    raise RuntimeError(
        "Cannot safely run xcursorgen: no temporary directory without whitespace is available"
    )


def _safe_theme_name(name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("theme name must not be empty")
    name = name.strip()
    if name in (".", "..") or os.path.basename(name) != name:
        raise ValueError(f"invalid theme name: {name!r}")
    if os.sep in name or (os.altsep and os.altsep in name):
        raise ValueError(f"invalid theme name: {name!r}")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError(f"invalid theme name: contains control characters")
    return name


def _safe_display_name(name):
    """Looser than _safe_theme_name: for replace-in-place, this string is
    only ever written into index.theme's Name= line, never used to build a
    directory path, so slashes and the like are harmless here."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("theme name must not be empty")
    name = name.strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError("invalid theme name: contains control characters")
    return name


def _resolve_alias(cursors_dir, name):
    """Resolve a cursor alias chain to its terminal file within cursors/."""
    current = name
    seen = set()
    root = os.path.realpath(cursors_dir)

    for _ in range(MAX_ALIAS_DEPTH):
        if current in seen:
            raise ValueError(f"cursor alias cycle detected at {name!r}")
        seen.add(current)

        path = os.path.join(cursors_dir, current)
        if not os.path.islink(path):
            if not os.path.isfile(path):
                raise ValueError(f"cursor alias {name!r} targets missing file {current!r}")
            resolved = os.path.realpath(path)
            if os.path.commonpath((root, resolved)) != root:
                raise ValueError(f"cursor alias {name!r} resolves outside its cursors directory")
            return current

        target = os.readlink(path)
        target_path = os.path.realpath(os.path.join(os.path.dirname(path), target))
        if os.path.commonpath((root, target_path)) != root:
            raise ValueError(f"cursor alias {name!r} targets outside its cursors directory")
        current = os.path.relpath(target_path, root)
        if os.sep in current:
            raise ValueError(f"cursor alias {name!r} targets a nested path")

    raise ValueError(f"cursor alias chain for {name!r} exceeds {MAX_ALIAS_DEPTH} links")


def _copy_original_cursor(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def _build_index_theme(src_path, dst_path, theme_name):
    """Copy index.theme while replacing every Icon Theme Name entry."""
    src = os.path.join(src_path, "index.theme")
    lines = []
    in_icon_theme = False
    replaced = False

    if os.path.isfile(src):
        # surrogateescape keeps any non-UTF-8 bytes (old Latin-1 comments etc.)
        # intact instead of aborting the whole theme upgrade.
        with open(src, encoding="utf-8", errors="surrogateescape") as f:
            for raw in f:
                line = raw.rstrip("\n")
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    in_icon_theme = stripped.lower() == "[icon theme]"
                    lines.append(line)
                    continue
                key = stripped.split("=", 1)[0].strip().lower() if "=" in stripped else ""
                if in_icon_theme and (key == "name" or key.startswith("name[")):
                    if not replaced:
                        lines.append(f"Name={theme_name}")
                        replaced = True
                    continue
                lines.append(line)

    header_at = next((i for i, l in enumerate(lines)
                      if l.strip().lower() == "[icon theme]"), None)
    if header_at is None:
        lines.insert(0, "[Icon Theme]")
        header_at = 0
    if not replaced:
        lines.insert(header_at + 1, f"Name={theme_name}")

    with open(dst_path, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")


def _copy_theme_extras(theme_dir, cursors_src, out_dir, info, final_out_dir=None):
    """Copy top-level theme files and non-cursor files without following symlinks."""
    out_cursors = os.path.join(out_dir, "cursors")
    known_names = {c["name"] for c in info["cursors"]}

    for entry in os.listdir(cursors_src):
        if entry in known_names:
            continue
        src = os.path.join(cursors_src, entry)
        dst = os.path.join(out_cursors, entry)
        if os.path.islink(src):
            target = os.readlink(src)
            os.symlink(target, dst)
        elif os.path.isfile(src):
            shutil.copy2(src, dst)

    for entry in os.listdir(theme_dir):
        if entry in ("cursors", "index.theme"):
            continue
        # cursors_scalable derived from the old bitmaps would be stale the
        # moment cursors/ is rebuilt (GNOME 51 prefers it over cursors/, so
        # it would silently undo the upscale) - the caller regenerates it.
        # Real vector art is carried through untouched.
        if entry == "cursors_scalable":
            # GNOME 51 does not search ~/.icons. A scaler copied into a legacy
            # output there would be inert; the post-processing GNOME step will
            # place it in the XDG companion and preserve vector art explicitly.
            if not gnome51.is_xdg_theme_dir(final_out_dir or out_dir):
                continue
            if gnome51.scalable_origin(theme_dir) != "vector":
                continue
        src = os.path.join(theme_dir, entry)
        dst = os.path.join(out_dir, entry)
        if os.path.islink(src):
            os.symlink(os.readlink(src), dst)
        elif os.path.isdir(src):
            shutil.copytree(src, dst, symlinks=True)
        elif os.path.isfile(src):
            shutil.copy2(src, dst)


def _commit_stage(stage_dir, out_dir):
    """Swap a staged directory into place (see fsutil.commit_dir)."""
    fsutil.commit_dir(stage_dir, out_dir)


def process_theme(theme_dir, out_dir, scale=4, backend="lanczos",
                   cursor_names=None, out_theme_name=None,
                   progress_cb=None, model_path=None, info=None):
    if shutil.which("xcursorgen") is None:
        raise RuntimeError(
            "xcursorgen is not installed or not on PATH; on Arch install xorg-xcursorgen"
        )

    if info is None:
        info = inspect_theme(theme_dir)
    theme_dir = os.path.realpath(theme_dir)
    out_dir = os.path.abspath(out_dir)
    if theme_dir == os.path.realpath(out_dir):
        # Replace-in-place is handled by staging a sibling and committing it.
        replace_in_place = True
    else:
        replace_in_place = False

    same_theme_id = os.path.basename(theme_dir) == os.path.basename(out_dir)
    if replace_in_place or same_theme_id:
        # The unified upgrade path uses the existing theme ID for a user-local
        # override of a system theme. The directory path has already been
        # validated separately, so this value is a display name, not a path.
        theme_name = _safe_display_name(out_theme_name or info["name"])
    else:
        theme_name = _safe_theme_name(out_theme_name or (info["name"] + " HD"))
    # Compare fully-resolved paths on both sides - out_dir is deliberately
    # left as abspath (not realpath) everywhere else so writes go through a
    # symlinked ~/.icons as the user intends, but a symlink could otherwise
    # be used to make this specific safety check misfire.
    if os.path.commonpath((theme_dir, os.path.realpath(out_dir))) == theme_dir and theme_dir != out_dir:
        raise ValueError("output theme cannot be placed inside the source theme")

    real_cursors = [c for c in info["cursors"] if not c["is_symlink"]]
    if cursor_names is not None:
        wanted = set(cursor_names)
        unknown = wanted - {c["name"] for c in real_cursors}
        if unknown:
            raise ValueError("unknown cursor names: " + ", ".join(sorted(unknown)))
        real_cursors = [c for c in real_cursors if c["name"] in wanted]
        if not real_cursors:
            raise ValueError("no real cursors selected")

    cursors_src = os.path.join(theme_dir, "cursors")
    parent = os.path.dirname(out_dir)
    os.makedirs(parent, exist_ok=True)
    stage_root = tempfile.mkdtemp(prefix=f".{os.path.basename(out_dir)}.stage-", dir=parent)
    stage_dir = os.path.join(stage_root, os.path.basename(out_dir))
    os.makedirs(os.path.join(stage_dir, "cursors"), exist_ok=True)

    processed_names = set()
    errors = []

    try:
        with tempfile.TemporaryDirectory(prefix="cursor-hd-", dir=_space_free_temp_parent()) as tmp:
            for cursor_index, c in enumerate(real_cursors):
                name = c["name"]
                src_path = os.path.join(cursors_src, name)
                # Cursor names are allowed to contain spaces. Keep all
                # intermediate filenames and directories sanitized because
                # xcursorgen's config syntax is whitespace-delimited.
                work_name = f"cursor_{cursor_index:04d}"
                cursor_tmp_dir = os.path.join(tmp, work_name)
                try:
                    frame_dir = os.path.join(cursor_tmp_dir, "frames")
                    up_dir = os.path.join(cursor_tmp_dir, "upscaled")
                    dump_cursor(src_path, frame_dir, work_name)
                    manifest_file = os.path.join(frame_dir, f"{work_name}.json")
                    up_manifest_path = upscale_manifest(
                        manifest_file, frame_dir, up_dir, scale, backend,
                        model_path=model_path,
                    )
                    out_cursor_path = os.path.join(stage_dir, "cursors", name)
                    rebuild_cursor(up_manifest_path, up_dir, out_cursor_path)
                    processed_names.add(name)
                    status = {"cursor": name, "status": "ok"}
                except Exception as e:
                    status = {"cursor": name, "status": "error", "error": str(e)}
                    errors.append(status)
                finally:
                    # Free this cursor's frames now; otherwise they pile up in
                    # /tmp (often tmpfs) until the whole theme finishes.
                    shutil.rmtree(cursor_tmp_dir, ignore_errors=True)
                if progress_cb:
                    progress_cb(status)

        if errors:
            detail = "; ".join(f"{e['cursor']}: {e['error']}" for e in errors)
            if backend == "realesrgan":
                raise RuntimeError(
                    "REALESRGAN_RUNTIME_FAILURE: conversion failed; destination was not changed: "
                    + detail
                )
            raise RuntimeError(f"conversion failed; destination was not changed: {detail}")

        out_cursors_dir = os.path.join(stage_dir, "cursors")
        for c in info["cursors"]:
            name = c["name"]
            dst = os.path.join(out_cursors_dir, name)
            if c["is_symlink"]:
                terminal = _resolve_alias(cursors_src, name)
                if terminal in processed_names:
                    os.symlink(terminal, dst)
                else:
                    _copy_original_cursor(os.path.join(cursors_src, terminal), os.path.join(out_cursors_dir, terminal))
                    os.symlink(terminal, dst)
            elif name not in processed_names:
                _copy_original_cursor(os.path.join(cursors_src, name), dst)

        _copy_theme_extras(theme_dir, cursors_src, stage_dir, info, final_out_dir=out_dir)
        _build_index_theme(theme_dir, os.path.join(stage_dir, "index.theme"), theme_name)
        # Some Xcursor themes have a second [Icon Theme] file named
        # cursor.theme. Do not leave its Name= pointing at the source theme
        # when the output name was changed. Preserve Inherits= and all other
        # source metadata, and materialize copied symlinks before editing.
        cursor_theme_path = os.path.join(stage_dir, "cursor.theme")
        if os.path.lexists(cursor_theme_path):
            fsutil.set_icon_theme_name(cursor_theme_path, theme_name)

        # If this is in-place replacement, the source and destination are the
        # same directory. The staged sibling is still safe to commit atomically.
        _commit_stage(stage_dir, out_dir)
        shutil.rmtree(stage_root, ignore_errors=True)

    except Exception:
        shutil.rmtree(stage_root, ignore_errors=True)
        raise

    return {
        "out_dir": out_dir,
        "processed": sorted(processed_names),
        "errors": [],
        "total_real_cursors": len(real_cursors),
        "replace_in_place": replace_in_place,
    }


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser()
    ap.add_argument("theme_dir")
    ap.add_argument("out_dir")
    ap.add_argument("--scale", type=int, default=4)
    ap.add_argument("--backend", default="lanczos", choices=["lanczos", "realesrgan"])
    ap.add_argument("--cursors", nargs="*", default=None)
    ap.add_argument("--name", default=None)
    ap.add_argument("--model-path", default=None)
    args = ap.parse_args()

    result = process_theme(
        args.theme_dir, args.out_dir, scale=args.scale, backend=args.backend,
        cursor_names=args.cursors, out_theme_name=args.name,
        progress_cb=lambda s: print(json.dumps(s)),
        model_path=args.model_path,
    )
    print(json.dumps(result, indent=2))
