#!/usr/bin/env python3
"""One coherent, batchable upgrade path for installed cursor themes."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import gnome51
import orchestrator
import fsutil
import model_utils

SCALE_FACTOR = 4



def _within(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath((os.path.realpath(path), os.path.realpath(parent))) == os.path.realpath(parent)
    except (ValueError, OSError):
        return False


def _xdg_icons_dir(home: str | None = None, xdg_data_home: str | None = None) -> str:
    if xdg_data_home is None:
        xdg_data_home = os.environ.get("XDG_DATA_HOME", os.path.join(home or str(Path.home()), ".local", "share"))
    return os.path.realpath(os.path.join(os.path.expanduser(xdg_data_home), "icons"))


def plan_theme_upgrade(theme_path: str, info: dict, home: str | None = None,
                       xdg_data_home: str | None = None) -> dict:
    """Plan a safe replacement without writing anything.

    Writable user themes are replaced in place. System themes and themes from
    other read-only locations become same-ID user-local overrides. A same-name
    unrelated XDG theme is never overwritten accidentally.
    """
    source = os.path.realpath(os.path.expanduser(theme_path))
    if not os.path.isdir(os.path.join(source, "cursors")):
        raise ValueError(f"{source} has no cursors/ directory")

    home_dir = os.path.realpath(os.path.expanduser(home or str(Path.home())))
    home_icons = os.path.realpath(os.path.join(home_dir, ".icons"))
    xdg_icons = _xdg_icons_dir(home=home_dir, xdg_data_home=xdg_data_home)
    source_parent = os.path.dirname(source)
    is_home_icons = source_parent == home_icons
    is_xdg_icons = source_parent == xdg_icons
    source_parent_writable = os.access(source_parent, os.W_OK)

    if (is_home_icons or is_xdg_icons) and source_parent_writable:
        out_dir = source
        mode = "replace in place"
        if is_home_icons and not is_xdg_icons:
            companion = os.path.join(xdg_icons, os.path.basename(source))
            if os.path.lexists(companion) and not gnome51.is_companion_for(companion, source):
                raise ValueError(
                    f"Cannot safely upgrade {info.get('name', os.path.basename(source))}: "
                    f"{companion} already contains a different theme. Resolve that theme-ID collision first."
                )
    else:
        out_dir = os.path.join(xdg_icons, os.path.basename(source))
        mode = "create user-local override"
        if os.path.realpath(out_dir) != source and os.path.lexists(out_dir):
            if not gnome51.is_companion_for(out_dir, source):
                raise ValueError(
                    f"Cannot safely upgrade {info.get('name', os.path.basename(source))}: "
                    f"{out_dir} already contains an unrelated theme with the same ID. "
                    "Nothing was changed."
                )
        parent = os.path.dirname(out_dir)
        if not os.path.isdir(parent) and not os.access(os.path.dirname(parent), os.W_OK):
            raise ValueError(f"Cannot create user-local override directory: {parent}")
        if os.path.isdir(parent) and not os.access(parent, os.W_OK):
            raise ValueError(f"User-local icon directory is not writable: {parent}")

    return {
        "source": source,
        "out_dir": os.path.abspath(out_dir),
        "mode": mode,
        "theme_id": os.path.basename(source),
        "name": info.get("name") or os.path.basename(source),
        "scalable_dir": os.path.join(gnome51.scalable_theme_target(out_dir), gnome51.SCALABLE_DIR),
    }


def _remove_path(path: str) -> None:
    if not os.path.lexists(path):
        return
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    else:
        shutil.rmtree(path)


def _copy_path(src: str, dst: str) -> None:
    if os.path.islink(src):
        os.symlink(os.readlink(src), dst)
    elif os.path.isdir(src):
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)


def _restore_path(backup: str | None, destination: str, originally_existed: bool) -> None:
    if originally_existed and backup and os.path.lexists(backup):
        if os.path.isdir(backup) and not os.path.islink(backup):
            fsutil.commit_dir(backup, destination)
        else:
            # Cursor theme destinations are normally directories; this branch
            # also makes rollback safe for an unexpected symlink/file target.
            parent = os.path.dirname(destination)
            os.makedirs(parent, exist_ok=True)
            _remove_path(destination)
            os.replace(backup, destination)
    elif not originally_existed:
        _remove_path(destination)


def upgrade_theme(theme_path: str, info: dict, progress_cb=None,
                  home: str | None = None, xdg_data_home: str | None = None,
                  backend: str = "lanczos", model_path: str | None = None) -> dict:
    """Upscale every cursor, rebuild its size ladder, and generate GNOME 51.

    Existing genuine vector shapes are snapshotted before processing and used
    to fill/preserve the scalable layer. The destination theme and any separate
    XDG scalable companion are backed up so a scalable-generation failure can
    roll back the theme instead of leaving a half-upgraded installation.
    """
    if backend not in ("lanczos", "realesrgan"):
        raise ValueError(f"unsupported upscale backend: {backend}")
    if backend == "realesrgan":
        if not model_path or not os.path.isfile(model_path):
            raise ValueError("Real-ESRGAN was selected but no readable supported model was found in ./weights")
    else:
        model_path = None

    plan = plan_theme_upgrade(theme_path, info, home=home, xdg_data_home=xdg_data_home)
    source = plan["source"]
    out_dir = plan["out_dir"]
    theme_name = plan["name"]
    out_parent = os.path.dirname(out_dir)
    os.makedirs(out_parent, exist_ok=True)

    original_out_exists = os.path.lexists(out_dir)
    rollback_root = tempfile.mkdtemp(prefix=f".{os.path.basename(out_dir)}.rollback-", dir=out_parent)
    output_backup = os.path.join(rollback_root, "theme") if original_out_exists else None
    if original_out_exists:
        _copy_path(out_dir, output_backup)

    scalable_theme_dir = gnome51.scalable_theme_target(out_dir)
    separate_scalable_target = os.path.realpath(scalable_theme_dir) != os.path.realpath(out_dir)
    original_scalable_theme_exists = os.path.lexists(scalable_theme_dir)
    scalable_dir = os.path.join(scalable_theme_dir, gnome51.SCALABLE_DIR)
    original_scalable_exists = separate_scalable_target and os.path.lexists(scalable_dir)
    scalable_backup_root = None
    scalable_backup = None
    if original_scalable_exists:
        scalable_backup_root = tempfile.mkdtemp(
            prefix=".cursor-hd-scalable-rollback-", dir=scalable_theme_dir
        )
        scalable_backup = os.path.join(scalable_backup_root, "cursors_scalable")
        _copy_path(scalable_dir, scalable_backup)

    preserve_temp = None
    preserve_dir = None
    try:
        source_scalable_theme = gnome51.existing_scalable_theme_dir(source)
        if source_scalable_theme and gnome51.scalable_origin(source) == "vector":
            source_scalable = os.path.join(source_scalable_theme, gnome51.SCALABLE_DIR)
            preserve_temp = tempfile.TemporaryDirectory(prefix="cursor-hd-vector-preserve-")
            preserve_dir = os.path.join(preserve_temp.name, gnome51.SCALABLE_DIR)
            shutil.copytree(source_scalable, preserve_dir, symlinks=True)

        def progress(entry):
            if progress_cb:
                payload = dict(entry)
                payload.setdefault("phase", "upscale")
                progress_cb(payload)

        result = orchestrator.process_theme(
            source,
            out_dir,
            scale=SCALE_FACTOR,
            backend=backend,
            cursor_names=None,
            out_theme_name=theme_name,
            progress_cb=progress,
            model_path=model_path,
            info=info,
        )
        if progress_cb:
            progress_cb({"phase": "gnome51", "cursor": "<scalable layer>", "status": "started"})
        g51_result = gnome51.generate_in_place(out_dir, preserve_dir=preserve_dir)
        result["gnome51"] = {
            "generated": len(g51_result.get("generated", [])),
            "aliases": len(g51_result.get("aliases", [])),
            "skipped": g51_result.get("skipped", []),
            "warnings": g51_result.get("warnings", []),
            "scalable_dir": g51_result.get("scalable_dir"),
        }
        result["mode"] = plan["mode"]
        result["source"] = source
        result["out_dir"] = out_dir
        result["theme_name"] = theme_name
        result["scalable_dir"] = g51_result.get("scalable_dir")
        result["scale_factor"] = SCALE_FACTOR
        result["backend"] = backend
        result["model_path"] = model_path
        return result
    except Exception as original_error:
        rollback_errors = []
        try:
            _restore_path(output_backup, out_dir, original_out_exists)
        except Exception as rollback_error:
            rollback_errors.append(f"theme rollback failed: {rollback_error}")
        if separate_scalable_target:
            try:
                if original_scalable_exists:
                    _restore_path(scalable_backup, scalable_dir, True)
                elif not original_scalable_theme_exists:
                    _remove_path(scalable_theme_dir)
                else:
                    _remove_path(scalable_dir)
            except Exception as rollback_error:
                rollback_errors.append(f"GNOME 51 rollback failed: {rollback_error}")
        if rollback_errors:
            raise RuntimeError(f"{original_error}; " + "; ".join(rollback_errors)) from original_error
        raise RuntimeError(f"{original_error}; original theme files were restored") from original_error
    finally:
        if preserve_temp is not None:
            preserve_temp.cleanup()
        shutil.rmtree(rollback_root, ignore_errors=True)
        if scalable_backup_root is not None:
            shutil.rmtree(scalable_backup_root, ignore_errors=True)

