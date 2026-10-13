import base64
import json
import os
import shutil
import struct
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

import gnome51
import scanner
import orchestrator
from unittest.mock import patch


IMAGE_TYPE = 0xFFFD0002


def write_minimal_xcursor(path: Path, bgra=(0, 0, 0, 255), nominal_size=24):
    """Write a valid 1x1 Xcursor file for isolated conversion tests."""
    header = b"Xcur" + struct.pack("<III", 16, 1, 1)
    toc = struct.pack("<III", IMAGE_TYPE, nominal_size, 28)
    chunk = struct.pack(
        "<IIIIIIIII", 36, IMAGE_TYPE, nominal_size, 1,
        1, 1, 0, 0, 0,
    ) + bytes(bgra)
    path.write_bytes(header + toc + chunk)


class Gnome51ConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cursor-hd-gnome51-test-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.xdg_data = self.home / ".local" / "share"
        self.theme = self.home / ".icons" / "Regression Theme"
        self.theme_cursors = self.theme / "cursors"
        self.theme_cursors.mkdir(parents=True)
        self.home.mkdir(parents=True, exist_ok=True)
        (self.theme / "index.theme").write_text(
            "[Icon Theme]\nName=Regression Theme\nComment=Fixture\n",
            encoding="utf-8",
        )
        write_minimal_xcursor(self.theme_cursors / "left_ptr", (0, 0, 255, 255))
        write_minimal_xcursor(self.theme_cursors / "xterm", (255, 255, 255, 255))
        write_minimal_xcursor(self.theme_cursors / "cross", (0, 255, 0, 255))
        os.symlink("left_ptr", self.theme_cursors / "arrow")
        os.symlink("left_ptr", self.theme_cursors / "00000000000000020006000e7e9ffc3f")

        self.old_home = os.environ.get("HOME")
        self.old_xdg = os.environ.get("XDG_DATA_HOME")
        self.old_search_dirs = scanner.SEARCH_DIRS
        os.environ["HOME"] = str(self.home)
        os.environ["XDG_DATA_HOME"] = str(self.xdg_data)
        scanner.SEARCH_DIRS = ["~/.icons", "~/.local/share/icons"]

    def tearDown(self):
        scanner.SEARCH_DIRS = self.old_search_dirs
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home
        if self.old_xdg is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = self.old_xdg
        self.temp.cleanup()

    def test_in_place_uses_xdg_companion_and_builds_css_aliases(self):
        result = gnome51.generate_in_place(str(self.theme))
        companion = self.xdg_data / "icons" / self.theme.name
        scalable = companion / "cursors_scalable"

        self.assertTrue(scalable.is_dir())
        self.assertTrue((scalable / "left_ptr" / "metadata.json").is_file())
        self.assertTrue(os.path.islink(companion / "cursors"))
        self.assertEqual(os.path.realpath(companion / "cursors"), os.path.realpath(self.theme_cursors))
        self.assertTrue(os.path.islink(scalable / "00000000000000020006000e7e9ffc3f"))
        self.assertTrue(os.path.islink(scalable / "default"))
        self.assertTrue(os.path.islink(scalable / "pointer"))

        # Confirm the SVG is well-formed and its embedded data URI decodes.
        svg_path = scalable / "left_ptr" / "frame-0000.svg"
        root = ET.parse(svg_path).getroot()
        image = next(iter(root))
        uri = image.attrib["{http://www.w3.org/1999/xlink}href"]
        png = base64.b64decode(uri.split(",", 1)[1])
        from io import BytesIO
        with Image.open(BytesIO(png)) as decoded:
            decoded.load()
            self.assertEqual(decoded.size, (1, 1))

        validation = gnome51.validate_scalable_theme(str(self.theme))
        self.assertIn(validation["status"], {"partial", "compatible"})
        self.assertEqual(validation["scalable_dir"], str(scalable))
        self.assertIn("dnd-ask", validation["missing"])  # no dnd-copy source exists

    def test_scanner_deduplicates_companion_theme(self):
        gnome51.generate_in_place(str(self.theme))
        matches = [path for path, _name in scanner.find_theme_dirs()
                   if Path(path).name == self.theme.name]
        self.assertEqual(len(matches), 1, matches)
        info = scanner.inspect_theme(str(self.theme))
        self.assertIn("gnome51_writable", info)
        self.assertIn(info["gnome51"]["status"], {"partial", "compatible"})

    def test_separate_copy_puts_scalable_layer_in_xdg_tree(self):
        (self.theme / "cursor.theme").write_text(
            "[Icon Theme]\nName=Old Display Name\nInherits=Regression Theme\n",
            encoding="utf-8",
        )
        output = self.home / ".icons" / "Regression Theme GNOME 51"
        result = gnome51.generate_theme(
            str(self.theme), str(output), theme_name="Regression Theme GNOME 51"
        )
        companion = self.xdg_data / "icons" / output.name
        self.assertTrue(output.is_dir())
        output_cursor_theme = (output / "cursor.theme").read_text(encoding="utf-8")
        self.assertIn("Name=Regression Theme GNOME 51", output_cursor_theme)
        self.assertIn("Inherits=Regression Theme", output_cursor_theme)
        self.assertNotIn("Name=Old Display Name", output_cursor_theme)
        self.assertTrue((companion / "cursors_scalable").is_dir())
        self.assertTrue(os.path.islink(companion / "cursors"))
        self.assertEqual(os.path.realpath(companion / "cursors"), os.path.realpath(output / "cursors"))
        self.assertEqual(result["scalable_dir"], str(companion / "cursors_scalable"))

    def test_existing_vector_cursor_is_preserved(self):
        first = gnome51.generate_in_place(str(self.theme))
        scalable = Path(first["scalable_dir"])
        vector_dir = scalable / "left_ptr"
        vector_svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
            'viewBox="0 0 24 24"><path d="M1 1 L22 22" stroke="black"/></svg>'
        )
        (vector_dir / "left_ptr.svg").write_text(vector_svg, encoding="utf-8")
        (vector_dir / "metadata.json").write_text(json.dumps([{
            "filename": "left_ptr.svg", "hotspot_x": 0, "hotspot_y": 0,
            "nominal_size": 24,
        }]), encoding="utf-8")

        result = gnome51.generate_in_place(str(self.theme), preserve_dir=str(scalable))
        final_svg = (scalable / "left_ptr" / "left_ptr.svg").read_text(encoding="utf-8")
        self.assertIn("<path", final_svg)
        self.assertNotIn("data:image/png;base64", final_svg)
        self.assertNotIn("left_ptr", result["generated"])

    def test_renamed_output_updates_cursor_theme_name(self):
        (self.theme / "cursor.theme").write_text(
            "[Icon Theme]\nName=Original Cursor Name\n"
            "Inherits=Empty-Butterfly-Blue\n",
            encoding="utf-8",
        )
        output = self.home / ".icons" / "Chosen Output Name"

        def fake_dump_cursor(src_path, frame_dir, name):
            os.makedirs(frame_dir, exist_ok=True)
            Path(frame_dir, f"{name}.json").write_text("{}", encoding="utf-8")

        def fake_upscale_manifest(manifest_file, frame_dir, up_dir, scale, backend, model_path=None):
            os.makedirs(up_dir, exist_ok=True)
            manifest = Path(up_dir, "manifest.json")
            manifest.write_text("{}", encoding="utf-8")
            return str(manifest)

        def fake_rebuild_cursor(up_manifest_path, up_dir, out_cursor_path):
            Path(out_cursor_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_cursor_path).write_bytes(b"fake-xcursor-output")

        with patch("orchestrator.shutil.which", return_value="/usr/bin/xcursorgen"), \
             patch("orchestrator.dump_cursor", side_effect=fake_dump_cursor), \
             patch("orchestrator.upscale_manifest", side_effect=fake_upscale_manifest), \
             patch("orchestrator.rebuild_cursor", side_effect=fake_rebuild_cursor):
            orchestrator.process_theme(
                str(self.theme), str(output), out_theme_name="Chosen Output Name"
            )

        output_cursor_theme = (output / "cursor.theme").read_text(encoding="utf-8")
        self.assertIn("Name=Chosen Output Name", output_cursor_theme)
        self.assertIn("Inherits=Empty-Butterfly-Blue", output_cursor_theme)
        self.assertNotIn("Name=Original Cursor Name", output_cursor_theme)

    def test_theme_name_writer_does_not_edit_symlink_target(self):
        target = self.root / "original-cursor.theme"
        target.write_text(
            "[Icon Theme]\nName=Original\nInherits=Legacy\n",
            encoding="utf-8",
        )
        linked = self.root / "renamed" / "cursor.theme"
        linked.parent.mkdir(parents=True)
        os.symlink(target, linked)
        from fsutil import set_icon_theme_name
        set_icon_theme_name(str(linked), "Renamed Theme")

        self.assertFalse(linked.is_symlink())
        self.assertIn("Name=Renamed Theme", linked.read_text(encoding="utf-8"))
        self.assertIn("Name=Original", target.read_text(encoding="utf-8"))
        self.assertIn("Inherits=Legacy", linked.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
