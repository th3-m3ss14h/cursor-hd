#!/usr/bin/env python3
"""
Rebuild an Xcursor binary from an upscaled manifest via xcursorgen.

xcursorgen config line format (one per frame):
    <nominal_size> <xhot> <yhot> <path_to_png> [<delay_ms>]

Frames sharing a nominal_size are read in file order = animation order.
Multiple nominal_size groups get packed into a single output .cursor file
(this is how a theme supports crisp fallback at several sizes).

Usage:
    python3 xcur_rebuild.py <upscaled_manifest.json> <frame_dir> <out_cursor_file>
"""
import json
import os
import subprocess
import sys
import tempfile


def rebuild_cursor(manifest_path, frame_dir, out_path):
    with open(manifest_path) as f:
        manifest = json.load(f)

    lines = []
    # Sort nominal sizes ascending purely for a readable config; xcursorgen
    # doesn't care about order across size groups.
    for nominal_size in sorted(manifest["sizes"], key=int):
        for fr in manifest["sizes"][nominal_size]:
            png_path = os.path.abspath(os.path.join(frame_dir, fr["file"]))
            lines.append(
                f"{nominal_size} {fr['xhot']} {fr['yhot']} {png_path} {fr['delay']}"
            )

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".xcursorgen.cfg", delete=False
    ) as cfg:
        cfg.write("\n".join(lines) + "\n")
        cfg_path = cfg.name

    try:
        result = subprocess.run(
            ["xcursorgen", cfg_path, out_path],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"xcursorgen failed (rc={result.returncode})\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}\n"
                f"config was:\n{chr(10).join(lines)}"
            )
    finally:
        os.unlink(cfg_path)

    return out_path


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} <upscaled_manifest.json> <frame_dir> "
              f"<out_cursor_file>", file=sys.stderr)
        sys.exit(1)
    rebuild_cursor(*sys.argv[1:4])
    print(f"wrote {sys.argv[3]}")
