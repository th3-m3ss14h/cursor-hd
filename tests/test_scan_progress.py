import tempfile
import unittest
from pathlib import Path

import scanner


class ScanProgressTests(unittest.TestCase):
    def test_scan_reports_discovery_theme_and_cursor_progress(self):
        with tempfile.TemporaryDirectory() as raw:
            icons = Path(raw) / "icons"
            theme = icons / "DemoTheme"
            cursors = theme / "cursors"
            cursors.mkdir(parents=True)
            (theme / "index.theme").write_text(
                "[Icon Theme]\nName=Demo Theme\nInherits=hicolor\n",
                encoding="utf-8",
            )
            # Non-Xcursor file still exercises the per-entry progress path.
            (cursors / "odd-cursor-name").write_bytes(b"not-xcursor")

            old_dirs = scanner.SEARCH_DIRS
            events = []
            try:
                scanner.SEARCH_DIRS = [str(icons)]
                results = scanner.scan_all(progress_cb=events.append)
            finally:
                scanner.SEARCH_DIRS = old_dirs

            phases = [event.get("phase") for event in events]
            self.assertEqual(len(results), 1)
            self.assertIn("discovery", phases)
            self.assertIn("themes_found", phases)
            self.assertIn("theme_start", phases)
            self.assertIn("legacy_scan", phases)
            self.assertIn("theme_done", phases)
            self.assertEqual(phases[-1], "complete")
            cursor_event = next(event for event in events if event.get("phase") == "legacy_scan")
            self.assertEqual(cursor_event["cursor"], "odd-cursor-name")
            self.assertEqual(cursor_event["cursor_index"], 1)
            self.assertEqual(cursor_event["cursor_total"], 1)
            self.assertEqual(events[-1]["themes_done"], 1)

    def test_scan_without_callback_keeps_original_return_contract(self):
        with tempfile.TemporaryDirectory() as raw:
            icons = Path(raw) / "icons"
            theme = icons / "DemoTheme"
            (theme / "cursors").mkdir(parents=True)
            (theme / "index.theme").write_text(
                "[Icon Theme]\nName=Demo Theme\n", encoding="utf-8"
            )
            old_dirs = scanner.SEARCH_DIRS
            try:
                scanner.SEARCH_DIRS = [str(icons)]
                results = scanner.scan_all()
            finally:
                scanner.SEARCH_DIRS = old_dirs
            self.assertIsInstance(results, list)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["name"], "Demo Theme")


if __name__ == "__main__":
    unittest.main()
