#!/usr/bin/env python3
"""
Filesystem helpers shared by the cursor-upgrade pipeline and the GNOME 51 converter.

commit_dir() is the single place that swaps a staged directory into place.
Invariant: the destination is only ever removed after it has been renamed
aside successfully, so a failed rename can never cost the original.
"""
import os
import shutil
import uuid


def commit_dir(stage_dir, out_dir):
    """Replace out_dir with stage_dir. On any failure the original out_dir
    (if there was one) is left intact or restored."""
    out_dir = os.path.abspath(out_dir)
    parent = os.path.dirname(out_dir)
    os.makedirs(parent, exist_ok=True)

    backup = None
    try:
        if os.path.lexists(out_dir):
            candidate = os.path.join(
                parent, f".{os.path.basename(out_dir)}.backup-{uuid.uuid4().hex[:10]}"
            )
            os.replace(out_dir, candidate)  # if this raises, the original never moved
            backup = candidate              # only recorded once it is safely aside
        os.replace(stage_dir, out_dir)
    except Exception:
        if backup is not None and os.path.lexists(backup):
            if os.path.lexists(out_dir):    # defensive: rename is atomic, shouldn't exist
                shutil.rmtree(out_dir, ignore_errors=True)
            os.replace(backup, out_dir)     # put the original back
        raise
    else:
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)


def set_icon_theme_name(path, name):
    """Set Name= inside an icon-theme configuration file safely.

    If ``path`` is a symlink, read its current contents and replace the link
    with a regular file before writing. That prevents renaming a source theme
    through a symlink copied into a staged output directory.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("theme name must not be empty")
    name = name.strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError("theme name contains control characters")

    path = os.path.abspath(path)
    was_symlink = os.path.islink(path)
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    else:
        lines = []

    if was_symlink:
        os.unlink(path)
    elif not os.path.lexists(path):
        # The file may be absent in older themes. Callers decide whether the
        # optional file should be created; this helper is only called for
        # index.theme or an already-existing cursor.theme.
        pass

    in_icon_theme = False
    replaced = False
    header_at = None
    output = []
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_icon_theme = stripped.lower() == "[icon theme]"
            if in_icon_theme and header_at is None:
                header_at = len(output)
            output.append(raw)
            continue

        key = stripped.split("=", 1)[0].strip().lower() if "=" in stripped else ""
        if in_icon_theme and (key == "name" or key.startswith("name[")):
            if not replaced:
                output.append(f"Name={name}")
                replaced = True
            continue
        output.append(raw)

    if not replaced:
        if header_at is None:
            output.insert(0, "[Icon Theme]")
            header_at = 0
        output.insert(header_at + 1, f"Name={name}")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(output).rstrip("\n") + "\n")
    return True
