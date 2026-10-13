import os
import shutil
import tempfile
import unittest
import struct
from pathlib import Path
from unittest.mock import patch

import gnome51
import scanner
import upgrade


class UpgradePlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cursor-hd-plan-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.xdg_data = self.home / ".local" / "share"
        self.xdg_icons = self.xdg_data / "icons"
        self.home_icons = self.home / ".icons"
        self.system_icons = self.root / "usr" / "share" / "icons"
        for path in (self.xdg_icons, self.home_icons, self.system_icons):
            path.mkdir(parents=True, exist_ok=True)
        self.old_home = os.environ.get("HOME")
        self.old_xdg = os.environ.get("XDG_DATA_HOME")
        os.environ["HOME"] = str(self.home)
        os.environ["XDG_DATA_HOME"] = str(self.xdg_data)

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home
        if self.old_xdg is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = self.old_xdg
        self.temp.cleanup()

    def make_theme(self, parent, name):
        theme = parent / name
        (theme / "cursors").mkdir(parents=True)
        (theme / "index.theme").write_text(f"[Icon Theme]\nName={name} display\n", encoding="utf-8")
        return theme

    def test_writable_home_theme_is_replaced_in_place(self):
        theme = self.make_theme(self.home_icons, "Butterfly")
        plan = upgrade.plan_theme_upgrade(str(theme), {"name": "Butterfly display"})
        self.assertEqual(plan["out_dir"], str(theme))
        self.assertEqual(plan["mode"], "replace in place")
        self.assertEqual(plan["theme_id"], "Butterfly")

    def test_home_icons_symlink_to_xdg_icons_does_not_mistake_source_for_companion(self):
        theme = self.make_theme(self.xdg_icons, "SharedIconsTheme")
        shutil.rmtree(self.home_icons)
        self.home_icons.symlink_to(self.xdg_icons, target_is_directory=True)
        plan = upgrade.plan_theme_upgrade(str(theme), {"name": "Shared Icons Theme"})
        self.assertEqual(plan["out_dir"], str(theme.resolve()))
        self.assertEqual(plan["mode"], "replace in place")

    def test_system_theme_gets_same_id_user_override(self):
        theme = self.make_theme(self.system_icons, "Adwaita")
        plan = upgrade.plan_theme_upgrade(str(theme), {"name": "Adwaita"})
        self.assertEqual(plan["out_dir"], str(self.xdg_icons / "Adwaita"))
        self.assertEqual(plan["mode"], "create user-local override")
        self.assertFalse((self.xdg_icons / "Adwaita").exists())

    def test_unrelated_xdg_theme_collision_is_refused(self):
        theme = self.make_theme(self.system_icons, "Collision")
        self.make_theme(self.xdg_icons, "Collision")
        with self.assertRaisesRegex(ValueError, "unrelated theme"):
            upgrade.plan_theme_upgrade(str(theme), {"name": "Collision"})

    def test_existing_companion_for_source_is_allowed_as_output(self):
        theme = self.make_theme(self.system_icons, "SystemCursor")
        companion = self.xdg_icons / "SystemCursor"
        companion.mkdir()
        os.symlink(str(theme / "cursors"), companion / "cursors")
        os.symlink(str(theme / "index.theme"), companion / "index.theme")
        plan = upgrade.plan_theme_upgrade(str(theme), {"name": "SystemCursor"})
        self.assertEqual(plan["out_dir"], str(companion))
        self.assertEqual(plan["mode"], "create user-local override")

    def test_scan_collapses_same_id_user_override_and_system_source(self):
        user_theme = self.make_theme(self.xdg_icons, "SameID")
        system_theme = self.make_theme(self.system_icons, "SameID")
        old_search_dirs = scanner.SEARCH_DIRS
        try:
            scanner.SEARCH_DIRS = [str(self.xdg_icons), str(self.system_icons)]
            matches = scanner.find_theme_dirs()
        finally:
            scanner.SEARCH_DIRS = old_search_dirs
        paths = [path for path, _ in matches if Path(path).name == "SameID"]
        self.assertEqual(paths, [str(user_theme.resolve())])
        self.assertNotIn(str(system_theme.resolve()), paths)

    def test_scan_does_not_collapse_distinct_theme_ids_with_same_display_name(self):
        first = self.make_theme(self.xdg_icons, "ThemeA")
        second = self.make_theme(self.system_icons, "ThemeB")
        for theme in (first, second):
            (theme / "index.theme").write_text("[Icon Theme]\nName=Same display name\n", encoding="utf-8")
        old_search_dirs = scanner.SEARCH_DIRS
        try:
            scanner.SEARCH_DIRS = [str(self.xdg_icons), str(self.system_icons)]
            matches = scanner.find_theme_dirs()
        finally:
            scanner.SEARCH_DIRS = old_search_dirs
        paths = {path for path, _ in matches}
        self.assertIn(str(first.resolve()), paths)
        self.assertIn(str(second.resolve()), paths)


class EndToEndUpgradeTests(unittest.TestCase):
    IMAGE_TYPE = 0xFFFD0002

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cursor-hd-e2e-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.xdg_data = self.home / ".local" / "share"
        self.xdg_icons = self.xdg_data / "icons"
        self.home_icons = self.home / ".icons"
        self.system_icons = self.root / "usr" / "share" / "icons"
        self.bin_dir = self.root / "bin"
        for path in (self.xdg_icons, self.home_icons, self.system_icons, self.bin_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.old_home = os.environ.get("HOME")
        self.old_xdg = os.environ.get("XDG_DATA_HOME")
        self.old_path = os.environ.get("PATH", "")
        self.old_fake_source = os.environ.get("FAKE_XCURSOR_SOURCE")
        os.environ["HOME"] = str(self.home)
        os.environ["XDG_DATA_HOME"] = str(self.xdg_data)
        os.environ["PATH"] = str(self.bin_dir) + os.pathsep + self.old_path
        self.source_cursor = self.root / "fixture.cursor"
        self.write_cursor(self.source_cursor, nominal_size=24)
        fake = self.bin_dir / "xcursorgen"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import os, shutil, sys\n"
            "shutil.copyfile(os.environ['FAKE_XCURSOR_SOURCE'], sys.argv[2])\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        os.environ["FAKE_XCURSOR_SOURCE"] = str(self.source_cursor)
        self.old_search_dirs = scanner.SEARCH_DIRS
        scanner.SEARCH_DIRS = ["~/.icons", "~/.local/share/icons", str(self.system_icons)]

    def tearDown(self):
        scanner.SEARCH_DIRS = self.old_search_dirs
        for name, old in (("HOME", self.old_home), ("XDG_DATA_HOME", self.old_xdg),
                          ("PATH", self.old_path), ("FAKE_XCURSOR_SOURCE", self.old_fake_source)):
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
        self.temp.cleanup()

    def write_cursor(self, path: Path, nominal_size=24):
        width = height = 1
        header = b"Xcur" + struct.pack("<III", 16, 1, 1)
        toc = struct.pack("<III", self.IMAGE_TYPE, nominal_size, 28)
        chunk = struct.pack("<IIIIIIIII", 36, self.IMAGE_TYPE, nominal_size, 1,
                            width, height, 0, 0, 0) + bytes((0, 0, 255, 255))
        path.write_bytes(header + toc + chunk)

    def make_theme(self, parent, name):
        theme = parent / name
        cursors = theme / "cursors"
        cursors.mkdir(parents=True)
        (theme / "index.theme").write_text(
            f"[Icon Theme]\nName={name} Display\nComment=Fixture\n", encoding="utf-8"
        )
        (cursors / "left_ptr").write_bytes(self.source_cursor.read_bytes())
        os.symlink("left_ptr", cursors / "arrow")
        return theme

    def test_user_theme_is_upgraded_in_place_and_has_one_scanned_entry(self):
        theme = self.make_theme(self.home_icons, "Butterfly")
        info = scanner.inspect_theme(str(theme))
        result = upgrade.upgrade_theme(str(theme), info)
        companion = self.xdg_icons / "Butterfly"
        self.assertEqual(result["out_dir"], str(theme.resolve()))
        self.assertTrue((theme / "cursors" / "left_ptr").is_file())
        self.assertTrue((companion / "cursors_scalable" / "left_ptr" / "metadata.json").is_file())
        self.assertTrue((companion / "cursors_scalable" / "default").is_symlink())
        matches = [path for path, _ in scanner.find_theme_dirs() if Path(path).name == "Butterfly"]
        self.assertEqual(matches, [str(theme.resolve())])
        self.assertEqual((theme / "index.theme").read_text().splitlines()[1], "Name=Butterfly Display")

    def test_system_theme_becomes_same_id_user_override_and_is_not_duplicated(self):
        theme = self.make_theme(self.system_icons, "SystemAlpha")
        info = scanner.inspect_theme(str(theme))
        result = upgrade.upgrade_theme(str(theme), info)
        override = self.xdg_icons / "SystemAlpha"
        self.assertEqual(Path(result["out_dir"]), override)
        self.assertTrue((override / "cursors" / "left_ptr").is_file())
        self.assertTrue((override / "cursors_scalable" / "left_ptr" / "metadata.json").is_file())
        matches = [path for path, _ in scanner.find_theme_dirs() if Path(path).name == "SystemAlpha"]
        self.assertEqual(matches, [str(override.resolve())])
        self.assertTrue((theme / "cursors" / "left_ptr").is_file())



    def test_default_upgrade_uses_lanczos_even_if_a_model_is_installed(self):
        theme = self.make_theme(self.home_icons, "ExplicitLanczosTheme")
        info = scanner.inspect_theme(str(theme))
        scalable_result = {"generated": [], "aliases": [], "skipped": [], "warnings": [], "scalable_dir": str(theme / "cursors_scalable")}

        with patch("upgrade.model_utils.preferred_model_path", side_effect=AssertionError("must not auto-select a model")), \
             patch("upgrade.orchestrator.process_theme", return_value={"processed": ["left_ptr"]}) as process, \
             patch("upgrade.gnome51.generate_in_place", return_value=scalable_result):
            result = upgrade.upgrade_theme(str(theme), info)

        self.assertEqual(result["backend"], "lanczos")
        self.assertIsNone(result["model_path"])
        self.assertEqual(process.call_args.kwargs["backend"], "lanczos")
        self.assertIsNone(process.call_args.kwargs["model_path"])

    def test_selected_realesrgan_model_is_forwarded_to_the_processing_pipeline(self):
        theme = self.make_theme(self.home_icons, "ModelForwardTheme")
        info = scanner.inspect_theme(str(theme))
        weights = self.root / "weights"
        weights.mkdir()
        model = weights / "RealESRGAN_x4plus.pth"
        model.write_bytes(b"checkpoint path fixture")
        scalable_result = {"generated": [], "aliases": [], "skipped": [], "warnings": [], "scalable_dir": str(theme / "cursors_scalable")}

        with patch("upgrade.orchestrator.process_theme", return_value={"processed": ["left_ptr"]}) as process, \
             patch("upgrade.gnome51.generate_in_place", return_value=scalable_result):
            result = upgrade.upgrade_theme(str(theme), info, backend="realesrgan", model_path=str(model))

        self.assertEqual(result["backend"], "realesrgan")
        self.assertEqual(result["model_path"], str(model))
        self.assertEqual(process.call_args.kwargs["backend"], "realesrgan")
        self.assertEqual(process.call_args.kwargs["model_path"], str(model))

    def test_scalable_failure_rolls_back_changed_theme_directory(self):
        theme = self.make_theme(self.home_icons, "RollbackTheme")
        original_index = (theme / "index.theme").read_text(encoding="utf-8")
        info = scanner.inspect_theme(str(theme))

        def mutate_theme(*args, **kwargs):
            (theme / "index.theme").write_text("[Icon Theme]\nName=PARTIALLY UPGRADED\n", encoding="utf-8")
            return {"processed": ["left_ptr"], "out_dir": str(theme)}

        with patch("upgrade.orchestrator.process_theme", side_effect=mutate_theme), \
             patch("upgrade.gnome51.generate_in_place", side_effect=RuntimeError("synthetic scalable failure")):
            with self.assertRaisesRegex(RuntimeError, "original theme files were restored"):
                upgrade.upgrade_theme(str(theme), info)

        self.assertEqual((theme / "index.theme").read_text(encoding="utf-8"), original_index)
        self.assertFalse((self.xdg_icons / "RollbackTheme").exists())



if __name__ == "__main__":
    unittest.main()
