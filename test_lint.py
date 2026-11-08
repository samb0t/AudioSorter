"""Read-only destination tag checks; all fixtures live in temporary directories."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mutagen.id3 import ID3, TCON

from comments import lint_destinations


class LintTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.destinations = [{"directory": "Breaks", "genre": "Breaks"}]
        (self.root / "Breaks").mkdir()

    def track(self, name, genres=None):
        path = self.root / "Breaks" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio payload" * 100)
        if genres is not None:
            tags = ID3()
            tags.add(TCON(encoding=3, text=genres))
            tags.save(path)
        return path

    def test_missing_mismatched_and_multiple_genres_without_writes(self):
        good = self.track("good.mp3", ["Breaks"])
        missing = self.track("missing.mp3")
        wrong = self.track("nested/wrong.MP3", ["House"])
        multiple = self.track("multiple.mp3", ["Breaks", "House"])
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in [good, missing, wrong, multiple]}
        result = lint_destinations(self.root, self.destinations)
        self.assertEqual(result["scanned"], 4)
        self.assertEqual(result["checkedFolders"], 1)
        self.assertEqual(result["skippedFolders"], 0)
        issues = {item["file"]: item for item in result["issues"]}
        self.assertEqual(len(issues), 3)
        self.assertEqual(issues["Breaks/missing.mp3"]["problem"], "Missing Genre")
        self.assertEqual(issues["Breaks/nested/wrong.MP3"]["actual"], ["House"])
        self.assertEqual(issues["Breaks/multiple.mp3"]["actual"], ["Breaks", "House"])
        for item in issues.values():
            self.assertEqual(item["expected"], "Breaks")
            self.assertTrue(Path(item["path"]).is_absolute())
        for path, snapshot in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), snapshot)

    def test_clean_and_skipped_destinations(self):
        self.track("good.mp3", ["Breaks"])
        result = lint_destinations(self.root, self.destinations + ["Legacy", {"directory": "Review"}])
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["skippedFolders"], 2)
        self.assertEqual(result["issues"], [])

    def test_excludes_symlinks_hidden_and_unsupported_formats(self):
        self.track("good.mp3", ["Breaks"])
        self.track(".hidden.mp3")
        self.track(".hidden/nested.mp3")
        self.track("ignore.flac")
        self.track("ignore.m4a")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "wrong.mp3").write_bytes(b"audio")
        (self.root / "Breaks" / "linked.mp3").symlink_to(outside / "wrong.mp3")
        (self.root / "Breaks" / "linked-folder").symlink_to(outside, target_is_directory=True)
        result = lint_destinations(self.root, self.destinations)
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["issues"], [])

    def test_missing_and_symlink_destinations_are_reported(self):
        (self.root / "Link").symlink_to(self.root / "Breaks", target_is_directory=True)
        destinations = [{"directory": name, "genre": "Breaks"} for name in ["Absent", "Link"]]
        result = lint_destinations(self.root, destinations)
        self.assertEqual(result["scanned"], 0)
        self.assertEqual(len(result["issues"]), 2)

    def test_read_error_does_not_abort_scan(self):
        self.track("track.mp3", ["Breaks"])
        with patch("comments.tags_for", side_effect=OSError("Cannot read")):
            result = lint_destinations(self.root, self.destinations)
        self.assertEqual(result["scanned"], 1)
        self.assertIn("Cannot read", result["issues"][0]["problem"])

    def test_directory_scan_error_is_reported(self):
        def fail_walk(folder, **kwargs):
            kwargs["onerror"](PermissionError(13, "Permission denied", str(folder)))
            return iter(())

        with patch("comments.os.walk", side_effect=fail_walk):
            result = lint_destinations(self.root, self.destinations)
        self.assertIn("Cannot scan directory", result["issues"][0]["problem"])

    def test_cli_returns_report(self):
        self.track("good.mp3", ["Breaks"])
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("comments.py")), "lint", str(self.root)],
            input=json.dumps({"destinations": self.destinations}), capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["issues"], [])

    def test_rejects_destination_path_traversal(self):
        for name in ["..", "../outside", "/outside", ""]:
            with self.assertRaises(ValueError):
                lint_destinations(self.root, [{"directory": name, "genre": "Breaks"}])
