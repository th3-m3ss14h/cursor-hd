import json
import os
import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import orchestrator
import scanner
from local_security import local_api_request_error
from xcur_extract import scan_xcursor_toc
from xcur_upscale import upscale_manifest


class SuggestedFixRegressionTests(unittest.TestCase):
    def test_index_theme_comment_before_header_does_not_duplicate_section(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            (source / "index.theme").write_text(
                "# generated theme\n; another comment\n[Icon Theme]\n"
                "Name=Original\nInherits=hicolor\n",
                encoding="utf-8",
            )
            destination = root / "output" / "index.theme"
            destination.parent.mkdir(parents=True)
            orchestrator._build_index_theme(str(source), str(destination), "Renamed")
            result = destination.read_text(encoding="utf-8")
            self.assertEqual(result.lower().count("[icon theme]"), 1)
            self.assertIn("Name=Renamed", result)
            self.assertIn("Inherits=hicolor", result)

    def test_index_theme_preserves_non_utf8_bytes_without_aborting(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            (source / "index.theme").write_bytes(
                b"# legacy byte: \xff\n[Icon Theme]\nName=Original\n"
            )
            destination = root / "index.theme"
            orchestrator._build_index_theme(str(source), str(destination), "Renamed")
            content = destination.read_bytes()
            self.assertIn(b"\xff", content)
            self.assertIn(b"Name=Renamed", content)
            self.assertNotIn(b"Name=Original", content)

    def test_scanner_ignores_dot_prefixed_backup_directories(self):
        with tempfile.TemporaryDirectory() as raw:
            icons = Path(raw) / "icons"
            visible = icons / "VisibleTheme"
            backup = icons / ".VisibleTheme.backup-deadbeef"
            (visible / "cursors").mkdir(parents=True)
            (backup / "cursors").mkdir(parents=True)
            (visible / "index.theme").write_text(
                "[Icon Theme]\nName=Visible Theme\n", encoding="utf-8"
            )
            (backup / "index.theme").write_text(
                "[Icon Theme]\nName=Backup Theme\n", encoding="utf-8"
            )
            old_dirs = scanner.SEARCH_DIRS
            try:
                scanner.SEARCH_DIRS = [str(icons)]
                found = scanner.find_theme_dirs()
            finally:
                scanner.SEARCH_DIRS = old_dirs
            self.assertEqual([Path(path).name for path, _ in found], ["VisibleTheme"])

    def test_corrupt_toc_count_is_rejected_before_reading_declared_table(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "corrupt.cursor"
            path.write_bytes(b"Xcur" + struct.pack("<III", 16, 1, 0xFFFFFFFF))
            with self.assertRaisesRegex(ValueError, "truncated TOC"):
                scan_xcursor_toc(str(path))

    def test_short_xcursor_header_raises_clean_error(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "short.cursor"
            path.write_bytes(b"Xcur\x10\x00")
            with self.assertRaisesRegex(ValueError, "truncated Xcursor header"):
                scan_xcursor_toc(str(path))

    def test_local_api_rejects_cross_origin_and_plain_text_mutations(self):
        self.assertIsNotNone(local_api_request_error(
            "127.0.0.1:5057", "https://attacker.example", "POST", "text/plain"
        ))
        self.assertIsNotNone(local_api_request_error(
            "127.0.0.1:5057", None, "POST", "text/plain"
        ))
        self.assertIsNotNone(local_api_request_error(
            "attacker.example:5057", None, "POST", "application/json"
        ))

    def test_local_json_mutation_with_same_origin_is_allowed(self):
        self.assertIsNone(local_api_request_error(
            "127.0.0.1:5057", "http://127.0.0.1:5057", "POST", "application/json"
        ))
        self.assertIsNone(local_api_request_error(
            "localhost:5057", None, "POST", "application/json"
        ))

    def test_output_filenames_only_replace_the_final_extension(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            frame_dir = root / "frames"
            output_dir = root / "output"
            frame_dir.mkdir()
            output_dir.mkdir()
            Image.new("RGBA", (1, 1), (20, 40, 60, 255)).save(
                frame_dir / "cursor.png.middle.png"
            )
            manifest = {
                "source": "cursor theme",
                "sizes": {
                    "2": [{
                        "file": "cursor.png.middle.png", "width": 1, "height": 1,
                        "xhot": 0, "yhot": 0, "delay": 0,
                    }]
                },
            }
            manifest_path = frame_dir / "cursor.json.middle.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            out_manifest = upscale_manifest(
                str(manifest_path), str(frame_dir), str(output_dir), 1, "lanczos"
            )
            self.assertEqual(Path(out_manifest).name, "cursor.json.middle.x1.json")
            output_manifest = json.loads(Path(out_manifest).read_text(encoding="utf-8"))
            frame_name = output_manifest["sizes"]["2"][0]["file"]
            self.assertEqual(frame_name, "cursor.png.middle.sz2.f000.png")
            self.assertTrue((output_dir / frame_name).is_file())

    def test_cursor_names_with_spaces_use_safe_temporary_paths_and_clean_each_cursor(self):
        with tempfile.TemporaryDirectory(prefix="source theme with spaces ") as raw:
            root = Path(raw)
            source = root / "Source Theme"
            cursors = source / "cursors"
            cursors.mkdir(parents=True)
            (source / "index.theme").write_text(
                "[Icon Theme]\nName=Source Theme\n", encoding="utf-8"
            )
            names = ["busy cursor.png", "text cursor"]
            for name in names:
                (cursors / name).write_bytes(b"stub")
            info = {
                "name": "Source Theme",
                "cursors": [{"name": name, "is_symlink": False} for name in names],
            }
            seen_frame_dirs = []
            seen_manifest = []

            def fake_dump(src_path, frame_dir, cursor_name):
                self.assertFalse(any(ch.isspace() for ch in frame_dir))
                self.assertFalse(any(ch.isspace() for ch in cursor_name))
                os.makedirs(frame_dir, exist_ok=True)
                Path(frame_dir, f"{cursor_name}.json").write_text("{}", encoding="utf-8")
                if seen_frame_dirs:
                    self.assertFalse(Path(seen_frame_dirs[-1]).parent.exists())
                seen_frame_dirs.append(frame_dir)

            def fake_upscale(manifest_path, frame_dir, out_dir, scale, backend, model_path=None):
                self.assertFalse(any(ch.isspace() for ch in manifest_path + frame_dir + out_dir))
                os.makedirs(out_dir, exist_ok=True)
                output = Path(out_dir) / "manifest.x4.json"
                output.write_text("{}", encoding="utf-8")
                seen_manifest.append(manifest_path)
                return str(output)

            def fake_rebuild(manifest_path, frame_dir, out_path):
                Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                Path(out_path).write_bytes(b"rebuilt")

            with patch("orchestrator.shutil.which", return_value="/usr/bin/xcursorgen"), \
                 patch("orchestrator.dump_cursor", side_effect=fake_dump), \
                 patch("orchestrator.upscale_manifest", side_effect=fake_upscale), \
                 patch("orchestrator.rebuild_cursor", side_effect=fake_rebuild):
                result = orchestrator.process_theme(
                    str(source), str(root / "Destination Theme"), info=info,
                    out_theme_name="Destination Theme",
                )

            self.assertEqual(result["processed"], sorted(names))
            self.assertEqual(len(seen_manifest), 2)
            for name in names:
                self.assertTrue((root / "Destination Theme" / "cursors" / name).is_file())

    def test_real_rebuild_config_handles_space_names_with_xcursorgen_stub(self):
        """Exercise extraction, upscaling and the real config writer end to end."""
        with tempfile.TemporaryDirectory(prefix="source theme with spaces ") as raw:
            root = Path(raw)
            source = root / "Source Theme"
            cursors = source / "cursors"
            cursors.mkdir(parents=True)
            (source / "index.theme").write_text(
                "[Icon Theme]\nName=Source Theme\n", encoding="utf-8"
            )

            image_type = 0xFFFD0002
            def write_xcursor(path):
                header = b"Xcur" + struct.pack("<III", 16, 1, 1)
                toc = struct.pack("<III", image_type, 4, 28)
                chunk = struct.pack(
                    "<IIIIIIIII", 36, image_type, 4, 1, 1, 1, 0, 0, 0
                ) + bytes((0, 0, 255, 255))
                path.write_bytes(header + toc + chunk)

            names = ["busy cursor.png", "text cursor"]
            for name in names:
                write_xcursor(cursors / name)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            checked_marker = root / "xcursorgen-checked.txt"
            fake_xcursorgen = bin_dir / "xcursorgen"
            fake_xcursorgen.write_text(
                "#!/usr/bin/env python3\n"
                "import os, pathlib, sys\n"
                "cfg, out = sys.argv[1], sys.argv[2]\n"
                "lines = [line.split() for line in open(cfg, encoding='utf-8') if line.strip()]\n"
                "if not lines or any(len(parts) != 5 for parts in lines):\n"
                "    print('invalid whitespace-delimited config fields'); sys.exit(31)\n"
                "for parts in lines:\n"
                "    png = parts[3]\n"
                "    if any(ch.isspace() for ch in png) or not os.path.isfile(png):\n"
                "        print('unsafe or missing PNG path: ' + png); sys.exit(32)\n"
                "    cursor_tmp = pathlib.Path(png).parent.parent\n"
                "    temp_root = cursor_tmp.parent\n"
                "    if cursor_tmp.name == 'cursor_0001' and (temp_root / 'cursor_0000').exists():\n"
                "        print('previous cursor temporary files were not cleaned'); sys.exit(33)\n"
                f"pathlib.Path({str(checked_marker)!r}).write_text('ok', encoding='utf-8')\n"
                "pathlib.Path(out).write_bytes(b'rebuilt-xcursor-fixture')\n",
                encoding="utf-8",
            )
            fake_xcursorgen.chmod(0o755)
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = str(bin_dir) + os.pathsep + old_path
            try:
                info = scanner.inspect_theme(str(source))
                result = orchestrator.process_theme(
                    str(source), str(root / "Destination Theme"),
                    out_theme_name="Destination Theme", info=info,
                )
            finally:
                os.environ["PATH"] = old_path

            self.assertTrue(checked_marker.is_file())
            self.assertEqual(result["processed"], sorted(names))
            for name in names:
                self.assertTrue((root / "Destination Theme" / "cursors" / name).is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
