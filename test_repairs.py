"""Confirmed Genre operations; disposable fixtures only."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mutagen.id3 import TCON

from comments import (AudioMetadata, WAV_GENRE_CONFLICT, genre_values, lint_destinations,
                      recheck_genres, repair_genres, repair_missing_genres, synchsafe, write_comment, write_genre)
from test_formats import fixture, rich_tags


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "House/nested").mkdir(parents=True)
        self.config = {"destinationRoot": str(self.root), "destinations": [
            {"directory": "House", "genre": "House"}]}

    def scan(self):
        return lint_destinations(self.root, self.config["destinations"])["issues"]

    def repair(self, findings):
        return repair_missing_genres(self.root, findings, lambda: self.config)["results"]

    def repair_all(self, findings):
        return repair_genres(self.root, findings, lambda: self.config)["results"]

    def conflict(self, name="conflict.wav", riff="Breaks", values=("Techno",)):
        path = self.root / "House/nested" / name
        tags = rich_tags()
        tags.add(TCON(encoding=1, text=list(values)))
        info = [(b"ICMT", b"Keep comment\0"), (b"INAM", b"Keep title\0")]
        if riff is not None:
            info.append((b"IGNR", (riff + "\0").encode("ascii")))
        fixture(path, tags, info)
        return path

    def test_targeted_recheck_reads_only_attempted_files_without_walking(self):
        repaired = self.conflict("repaired.wav")
        failed = self.conflict("failed.wav")
        untouched = self.conflict("untouched.wav")
        findings = [f for f in self.scan() if f["path"] != str(untouched)]
        write_genre(repaired, {"genre": "House"})
        write_comment(failed, "Changed since scan")
        with patch("comments.os.walk", side_effect=AssertionError("Must not walk")), \
                patch("comments.genre_values", wraps=genre_values) as reads:
            report = recheck_genres(self.root, findings, lambda: self.config)
        self.assertEqual(set(report["checkedPaths"]), {str(repaired), str(failed)})
        self.assertEqual([call.args[0] for call in reads.call_args_list], [Path(f["path"]) for f in findings])
        self.assertEqual(len(report["issues"]), 1)
        refreshed = report["issues"][0]
        self.assertEqual(refreshed["path"], str(failed))
        self.assertTrue(refreshed["repairable"])
        old = next(f for f in findings if f["path"] == str(failed))
        self.assertNotEqual(refreshed["snapshot"], old["snapshot"])
        self.assertEqual(self.repair_all([refreshed])[0]["status"], "success")

    def test_targeted_recheck_uses_current_mapping_and_reports_missing_files(self):
        path = self.conflict()
        findings = self.scan()
        self.config["destinations"][0]["genre"] = "Techno"
        report = recheck_genres(self.root, findings, lambda: self.config)
        self.assertEqual(report["issues"][0]["expected"], "Techno")
        path.unlink()
        report = recheck_genres(self.root, findings, lambda: self.config)
        self.assertEqual(report["checkedPaths"], [str(path)])
        self.assertFalse(report["issues"][0]["repairable"])
        self.assertIn("Cannot recheck file", report["issues"][0]["problem"])
        self.config["destinations"] = []
        report = recheck_genres(self.root, findings, lambda: self.config)
        self.assertIn("Genre mapping changed", report["issues"][0]["problem"])
        self.config["destinationRoot"] = str(self.root / "elsewhere")
        with self.assertRaisesRegex(ValueError, "Destination root changed"):
            recheck_genres(self.root, findings, lambda: self.config)

    def test_targeted_recheck_does_not_read_symlink_or_outside_paths(self):
        path = self.conflict()
        finding = self.scan()[0]
        outside = self.root / "outside.wav"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        with patch("comments.genre_values", side_effect=AssertionError("Unsafe read")) as reads:
            report = recheck_genres(self.root, [finding, dict(finding, path=str(outside))], lambda: self.config)
        reads.assert_not_called()
        self.assertEqual(len(report["issues"]), 2)
        self.assertTrue(all(not f["repairable"] for f in report["issues"]))

    def test_cli_targeted_recheck(self):
        path = self.conflict()
        findings = self.scan()
        write_genre(path, {"genre": "House"})
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(self.config))
        completed = subprocess.run([sys.executable, str(Path(__file__).with_name("comments.py")),
                                    "recheck-genres", str(self.root)],
                                   input=json.dumps({"findings": findings, "configurationPath": str(config_path)}),
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"checkedPaths": [str(path)], "issues": []})

    def test_wav_conflicts_synchronize_both_tags_and_preserve_other_bytes(self):
        for name, riff, values in [
            ("id3-only.wav", None, ("House",)),
            ("empty-riff.wav", "", ("Techno",)),
            ("riff-correct.wav", "House", ("Breaks",)),
            ("id3-correct.wav", "Breaks", ("House",)),
            ("neither-correct.WAV", "Breaks", ("Techno", "Minimal")),
        ]:
            self.conflict(name, riff, values)
        before = {p: AudioMetadata(p) for p in (self.root / "House/nested").iterdir()}
        findings = self.scan()
        self.assertEqual(len(findings), 5)
        self.assertTrue(all(f["repairable"] and f["problem"] == WAV_GENRE_CONFLICT for f in findings))
        results = self.repair_all(findings)
        self.assertEqual([r["status"] for r in results], ["success"] * 5)
        self.assertTrue(all("synchronized" in r["detail"] for r in results))
        self.assertEqual(self.scan(), [])
        for path, old in before.items():
            new = AudioMetadata(path)
            self.assertEqual(genre_values(path), (["House"], None))
            self.assertEqual(new.info_value(b"IGNR"), b"House\0")
            self.assertEqual(new.block.tags()["TCON"].text, ["House"])
            self.assertEqual(new.block.version, old.block.version)
            self.assertEqual([f for f in new.block.frames if f[:4] != b"TCON"],
                             [f for f in old.block.frames if f[:4] != b"TCON"])
            self.assertEqual([raw for key, _, raw in new.parts if key.lower() not in (b"id3 ", b"list")],
                             [raw for key, _, raw in old.parts if key.lower() not in (b"id3 ", b"list")])
            self.assertEqual([raw for key, _, raw in new.info if key != b"IGNR"],
                             [raw for key, _, raw in old.info if key != b"IGNR"])
        self.assertTrue(all(r["status"] == "skipped" for r in self.repair_all(findings)))

    def test_mixed_repair_leaves_mismatches_and_malformed_tags_untouched(self):
        self.conflict()
        fixture(self.root / "House/missing.mp3")
        wrong = self.root / "House/wrong.wav"
        fixture(wrong, info=[(b"IGNR", b"Breaks\0")])
        bad = self.root / "House/bad.wav"
        fixture(bad, info=[(b"IGNR", b"unterminated")])
        before = {p: p.read_bytes() for p in (wrong, bad)}
        findings = self.scan()
        results = self.repair_all(findings)
        self.assertEqual(sorted(r["status"] for r in results), ["skipped", "skipped", "success", "success"])
        # Forging eligibility cannot turn a mismatch or a read error into a repair.
        for finding in self.scan():
            forged = dict(finding, repairable=True, problem=WAV_GENRE_CONFLICT)
            self.assertNotEqual(self.repair_all([forged])[0]["status"], "success")
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)

    def test_conflict_repair_revalidates_file_mapping_and_paths(self):
        path = self.conflict()
        finding = self.scan()[0]
        original = path.read_bytes()
        self.config["destinations"][0]["genre"] = "Other"
        self.assertEqual(self.repair_all([finding])[0]["status"], "skipped")
        self.config["destinations"][0]["genre"] = "House"
        self.config["destinationRoot"] = str(self.root / "other")
        self.assertEqual(self.repair_all([finding])[0]["status"], "skipped")
        self.config["destinationRoot"] = str(self.root)
        self.assertEqual(self.repair_all([dict(finding, actual=["forged"])])[0]["status"], "skipped")
        write_comment(path, "External edit")
        self.assertEqual(self.repair_all([finding])[0]["status"], "skipped")
        path.write_bytes(original)
        finding = self.scan()[0]
        outside = self.root / "outside.wav"
        outside.write_bytes(original)
        self.assertEqual(self.repair_all([dict(finding, path=str(outside))])[0]["status"], "skipped")
        path.unlink()
        path.symlink_to(outside)
        self.assertEqual(self.repair_all([finding])[0]["status"], "skipped")
        self.assertEqual(outside.read_bytes(), original)

    def test_conflict_failure_and_commit_revalidation_are_atomic(self):
        path = self.conflict()
        findings = self.scan()
        original = path.read_bytes()
        with patch("comments.os.replace", side_effect=OSError("disk full")):
            self.assertEqual(self.repair_all(findings)[0]["status"], "failed")
        self.assertEqual(path.read_bytes(), original)
        def change_mapping(fd):
            self.config["destinations"][0]["genre"] = "Other"
        with patch("comments.os.fsync", side_effect=change_mapping):
            self.assertEqual(self.repair_all(findings)[0]["status"], "skipped")
        self.assertEqual(path.read_bytes(), original)
        self.config["destinations"][0]["genre"] = "House"
        import comments
        fsync = comments.os.fsync
        def external_edit(fd):
            with patch("comments.os.fsync", side_effect=fsync):
                write_genre(path, {"genre": "External"})
        with patch("comments.os.fsync", side_effect=external_edit):
            self.assertEqual(self.repair_all(findings)[0]["status"], "failed")
        self.assertEqual(genre_values(path), (["External"], None))
        self.assertFalse(list(self.root.rglob(".audio-sorter-*")))

    def test_conflict_unicode_mapping_fails_without_modifying_file(self):
        path = self.conflict()
        self.config["destinations"][0]["genre"] = "Électronique"
        findings = self.scan()
        original = path.read_bytes()
        self.assertEqual(self.repair_all(findings)[0]["status"], "failed")
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(list(self.root.rglob(".audio-sorter-*")))

    def test_cli_conflict_repair(self):
        path = self.conflict()
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(self.config))
        completed = subprocess.run([sys.executable, str(Path(__file__).with_name("comments.py")),
                                    "repair-genres", str(self.root)],
                                   input=json.dumps({"findings": self.scan(), "configurationPath": str(config_path)}),
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["results"][0]["status"], "success")
        self.assertEqual(genre_values(path), (["House"], None))

    def test_all_formats_absent_empty_nested_and_preservation(self):
        for ext in [".mp3", ".wav", ".aif", ".aiff"]:
            for state in ["absent", "empty"]:
                path = self.root / "House/nested" / (state + ext)
                tags = rich_tags()
                if state == "empty":
                    tags.add(TCON(encoding=1, text=[""]))
                fixture(path, tags, [(b"IGNR", b"\0"), (b"INAM", b"Title\0")] if ext == ".wav" else ())
                if state == "empty":
                    # Mutagen omits empty TCON on save: install an actual empty
                    # frame so this is not merely another absent-field fixture.
                    metadata = AudioMetadata(path)
                    size = synchsafe(2) if metadata.block.version == 4 else (2).to_bytes(4, "big")
                    metadata.block.replace(b"TCON", b"TCON" + size + b"\0\0\0\0")
                    metadata.save(edit_id3=True)
                    self.assertEqual(AudioMetadata(path).block.tags()["TCON"].text, [""])
        before = {p: AudioMetadata(p) for p in (self.root / "House/nested").iterdir()}
        findings = self.scan()
        self.assertEqual(len(findings), 8)
        self.assertTrue(all(f["repairable"] for f in findings))
        self.assertEqual([r["status"] for r in self.repair(findings)], ["success"] * 8)
        self.assertEqual(self.scan(), [])
        for path, old in before.items():
            new = AudioMetadata(path)
            self.assertEqual(genre_values(path), (["House"], None))
            self.assertEqual([f for f in old.block.frames if f[:4] != b"TCON"],
                             [f for f in new.block.frames if f[:4] != b"TCON"])
            if path.suffix == ".mp3":
                self.assertEqual(new.audio, old.audio)
            else:
                self.assertEqual([raw for key, _, raw in new.parts if key.lower() not in (b"id3 ", b"list")],
                                 [raw for key, _, raw in old.parts if key.lower() not in (b"id3 ", b"list")])
            if new.wav:
                self.assertEqual(new.info_value(b"INAM"), old.info_value(b"INAM"))
        self.assertTrue(all(r["status"] == "skipped" for r in self.repair(findings)))

    def test_changed_mapping_tags_comment_identity_and_root_skip(self):
        path = self.root / "House/nested/track.mp3"
        fixture(path)
        findings = self.scan()
        original = path.read_bytes()
        self.config["destinations"][0]["genre"] = "Breaks"
        self.assertEqual(self.repair(findings)[0]["status"], "skipped")
        self.config["destinations"][0]["genre"] = "House"
        self.config["destinationRoot"] = str(self.root / "other")
        self.assertEqual(self.repair(findings)[0]["status"], "skipped")
        self.config["destinationRoot"] = str(self.root)
        write_genre(path, {"genre": "Breaks"})
        self.assertEqual(self.repair(findings)[0]["status"], "skipped")
        self.assertEqual(genre_values(path), (["Breaks"], None))
        path.write_bytes(original)
        findings = self.scan()
        write_comment(path, "Other edit")
        self.assertEqual(self.repair(findings)[0]["status"], "skipped")
        path.write_bytes(original)
        findings = self.scan()
        replacement = path.with_name("replacement.mp3")
        replacement.write_bytes(original)
        replacement.replace(path)
        self.assertEqual(self.repair(findings)[0]["status"], "skipped")

    def test_empty_id3_only_wav_and_nonempty_id3_values(self):
        path = self.root / "House/empty-id3.wav"
        fixture(path, rich_tags())
        metadata = AudioMetadata(path)
        metadata.block.replace(b"TCON", b"TCON\0\0\0\2\0\0\0\0")
        metadata.save(edit_id3=True)
        self.assertIsNone(AudioMetadata(path).info_value(b"IGNR"))
        self.assertEqual(self.repair(self.scan())[0]["status"], "success")
        self.assertEqual(AudioMetadata(path).block.tags()["TCON"].text, ["House"])
        for ext in [".mp3", ".aif", ".aiff", ".wav"]:
            tags = rich_tags()
            tags.add(TCON(encoding=3, text=[" "]))
            wrong = self.root / "House" / ("nonempty" + ext)
            fixture(wrong, tags)
        findings = self.scan()
        self.assertEqual(len(findings), 4)
        self.assertEqual(sum(f["repairable"] for f in findings), 1)  # WAV ID3-only conflict.
        self.assertTrue(all(r["status"] == "skipped" for r in self.repair(findings)))

    def test_legacy_missing_only_repair_rejects_conflicts_and_other_issues(self):
        folder = self.root / "House"
        for name, riff, id3 in [("id3-only", None, "House"), ("conflict", "Breaks", "House"),
                                 ("wrong", "Breaks", None), ("space", " ", None)]:
            tags = rich_tags()
            if id3:
                tags.add(TCON(encoding=3, text=[id3]))
            fixture(folder / (name + ".wav"), tags, [(b"IGNR", (riff + "\0").encode())] if riff is not None else ())
        fixture(folder / "bad.wav", info=[(b"IGNR", b"no terminator")])
        fixture(folder / "bad-comment.wav", info=[(b"ICMT", b"no terminator")])
        fixture(folder / "truncated.aiff")
        (folder / "truncated.aiff").write_bytes((folder / "truncated.aiff").read_bytes()[:-1])
        self.config["destinations"].append({"directory": "Absent", "genre": "House"})
        before = {p: p.read_bytes() for p in folder.iterdir() if p.is_file()}
        findings = self.scan()
        self.assertEqual(len(findings), 8)
        self.assertEqual(sum(f["repairable"] for f in findings), 2)
        self.assertTrue(all(r["status"] == "skipped" for r in self.repair(findings)))
        self.assertFalse((self.root / "Absent").exists())
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)
        path = folder / "unreadable.mp3"
        fixture(path)
        findings = [f for f in self.scan() if f["path"] == str(path)]
        with patch("comments.AudioMetadata", side_effect=PermissionError("Cannot read")):
            self.assertEqual(self.repair(findings)[0]["status"], "failed")
        self.assertEqual(genre_values(path), ([], None))

    def test_symlinks_path_escapes_and_missing_files_skip(self):
        path = self.root / "House/nested/track.mp3"
        fixture(path)
        finding = self.scan()[0]
        outside = self.root / "outside.mp3"
        fixture(outside)
        path.unlink()
        path.symlink_to(outside)
        self.assertEqual(self.repair([finding])[0]["status"], "skipped")
        path.unlink()
        path.parent.rmdir()
        outside_dir = self.root / "outside"
        outside_dir.mkdir()
        fixture(outside_dir / "track.mp3")
        path.parent.symlink_to(outside_dir, target_is_directory=True)
        self.assertEqual(self.repair([finding])[0]["status"], "skipped")
        for malicious in [str(outside), str(self.root / "House/../outside.mp3"), str(self.root / "missing.mp3")]:
            changed = dict(finding, path=malicious)
            self.assertEqual(self.repair([changed])[0]["status"], "skipped")
        self.assertEqual(genre_values(outside), ([], None))
        self.assertEqual(genre_values(outside_dir / "track.mp3"), ([], None))

    def test_symlink_configured_root_and_removed_file_skip(self):
        path = self.root / "House/track.mp3"
        fixture(path)
        findings = self.scan()
        path.unlink()
        self.assertEqual(self.repair(findings)[0]["status"], "skipped")
        fixture(path)
        link = self.root / "linked-root"
        link.symlink_to(self.root, target_is_directory=True)
        report = lint_destinations(link, self.config["destinations"])
        self.assertFalse(any(f["repairable"] for f in report["issues"]))
        self.config["destinationRoot"] = str(link)
        changed = dict(findings[0], path=str(link / "House/track.mp3"))
        result = repair_missing_genres(link, [changed], lambda: self.config)
        self.assertEqual(result["results"][0]["status"], "skipped")
        self.assertEqual(genre_values(path), ([], None))

    def test_partial_failures_atomic_cleanup_and_commit_time_revalidation(self):
        for name in ["a.mp3", "b.wav", "c.aiff"]:
            fixture(self.root / "House" / name, rich_tags())
        findings = self.scan()
        import comments
        replace = comments.os.replace
        def fail_one(source, target):
            if Path(target).suffix == ".wav":
                raise OSError("disk full")
            return replace(source, target)
        with patch("comments.os.replace", side_effect=fail_one):
            results = self.repair(findings)
        self.assertEqual([r["status"] for r in results], ["success", "failed", "success"])
        self.assertFalse(list(self.root.rglob(".audio-sorter-*")))
        findings = self.scan()
        original = Path(findings[0]["path"]).read_bytes()
        # Settings can change while the verified temporary file is being written.
        def change_mapping(fd):
            self.config["destinations"][0]["genre"] = "Breaks"
        with patch("comments.os.fsync", side_effect=change_mapping):
            self.assertEqual(self.repair(findings)[0]["status"], "skipped")
        self.assertEqual(Path(findings[0]["path"]).read_bytes(), original)
        self.assertFalse(list(self.root.rglob(".audio-sorter-*")))

    def test_concurrent_external_tag_edit_is_not_overwritten(self):
        path = self.root / "House/track.mp3"
        fixture(path)
        findings = self.scan()
        import comments
        fsync = comments.os.fsync
        def edit_during_write(fd):
            # Model an external editor committing after repair reads the file.
            with patch("comments.os.fsync", side_effect=fsync):
                write_genre(path, {"genre": "External"})
        with patch("comments.os.fsync", side_effect=edit_during_write):
            self.assertEqual(self.repair(findings)[0]["status"], "failed")
        self.assertEqual(genre_values(path), (["External"], None))
        self.assertFalse(list(self.root.rglob(".audio-sorter-*")))

    def test_cli_requires_separate_request_and_reloads_settings(self):
        path = self.root / "House/track.mp3"
        fixture(path)
        findings = self.scan()
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(self.config))
        completed = subprocess.run([sys.executable, str(Path(__file__).with_name("comments.py")),
                                    "repair-missing-genres", str(self.root)],
                                   input=json.dumps({"findings": findings, "configurationPath": str(config_path)}),
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["results"][0]["status"], "success")

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg unavailable")
    def test_real_audio_packets_unchanged_all_formats(self):
        for ext in [".mp3", ".wav", ".aif", ".aiff"]:
            path = self.root / "House" / ("real" + ext)
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=0.1", "-y", str(path)], check=True)
            def payload():
                return subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a", "-c:a", "copy", "-f", "data", "-"])
            before = payload()
            self.assertEqual(self.repair(self.scan())[0]["status"], "success")
            self.assertEqual(payload(), before)
            if ext == ".wav":
                metadata = AudioMetadata(path)
                metadata.block.replace(b"TCON", metadata.block.encode(TCON(encoding=3, text=["Breaks"])))
                metadata.save(edit_id3=True)
                self.assertEqual(self.repair_all(self.scan())[0]["status"], "success")
                self.assertEqual(genre_values(path), (["House"], None))
                self.assertEqual(payload(), before)
