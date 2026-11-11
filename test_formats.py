"""Lossless container/tag tests. Fixtures are created only in temporary folders."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

from mutagen.id3 import APIC, COMM, ID3, PRIV, TCON, TIT2

from comments import (AudioMetadata, TagBlock, chunk, genre_values,
                      lint_destinations, read_comment, synchsafe, write_comment, write_genre)


def fixture(path, tags=None, info=()):
    """Small real PCM WAVE/AIFF, or an MP3-shaped payload for raw frame tests."""
    pcm = bytes(range(256)) * 16
    if path.suffix.lower() == ".mp3":
        path.write_bytes(pcm)
        if tags is not None:
            tags.save(path)
        return
    wav = path.suffix.lower() == ".wav"
    endian = "little" if wav else "big"
    if wav:
        with wave.open(str(path), "wb") as stream:
            stream.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
            stream.writeframes(pcm)
        parts = path.read_bytes()[12:]
    else:
        # 80-bit IEEE extended representation of 44100 Hz.
        comm = b"\0\1" + (len(pcm) // 2).to_bytes(4, "big") + b"\0\x10" + bytes.fromhex("400eac44000000000000")
        parts = chunk(b"COMM", comm, endian) + chunk(b"SSND", b"\0" * 8 + pcm, endian)
    # Opaque DJ metadata, odd-sized chunk with nonzero alignment byte.
    parts += b"DJMD" + (3).to_bytes(4, endian) + b"dj!" + b"\x7f"
    if tags is not None:
        stream = io.BytesIO()
        tags.save(stream, v2_version=3, padding=lambda _: 0)
        parts += chunk(b"id3 " if wav else b"ID3 ", stream.getvalue(), endian)
    if info:
        parts += chunk(b"LIST", b"INFO" + b"".join(chunk(k, v, "little") for k, v in info), endian)
    body = (b"WAVE" if wav else b"AIFF") + parts
    path.write_bytes((b"RIFF" if wav else b"FORM") + len(body).to_bytes(4, endian) + body)


def rich_tags():
    tags = ID3()
    tags.add(TIT2(encoding=1, text=["Title"]))
    tags.add(APIC(encoding=0, mime="image/jpeg", type=3, desc="cover", data=b"artwork bytes"))
    tags.add(PRIV(owner="Serato Markers2", data=b"opaque DJ cues\0\xff"))
    tags.add(COMM(encoding=1, lang="eng", desc="iTunNORM", text=["normalization"]))
    return tags


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_read_genre_cli_is_read_only(self):
        for extension in [".mp3", ".wav", ".aif", ".aiff"]:
            for present in [False, True]:
                with self.subTest(extension=extension, present=present):
                    path = self.root / ("track" + extension)
                    tags = ID3()
                    if present:
                        tags.add(TCON(encoding=3, text=["House", "Deep House"]))
                    fixture(path, tags, [(b"IGNR", b"Breaks\0")] if extension == ".wav" and present else ())
                    before = path.read_bytes()
                    result = subprocess.run([sys.executable, str(Path(__file__).with_name("comments.py")), "read-genre", str(path)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                    data = json.loads(result.stdout)
                    # The container fixture uses ID3v2.3, which joins values with '/'.
                    expected = (["Breaks", "ID3: House/Deep House"] if extension == ".wav" else ["House", "Deep House"] if extension == ".mp3" else ["House/Deep House"]) if present else []
                    self.assertEqual(data["genres"], expected)
                    self.assertEqual(bool(data["warning"]), extension == ".wav" and present)
                    self.assertEqual(path.read_bytes(), before)

    def test_each_format_comments_genre_undo_and_verbatim_preservation(self):
        for extension in [".mp3", ".wav", ".aif", ".aiff"]:
            for present in [False, True]:
                with self.subTest(extension=extension, present=present):
                    path = self.root / ("track" + extension)
                    tags = rich_tags()
                    if present:
                        tags.add(TCON(encoding=1, text=["Old", "Other"]))
                    fixture(path, tags, [(b"INAM", b"Original\0"), (b"IGNR", b"Old RIFF\0")] if extension == ".wav" and present else ())
                    before = AudioMetadata(path)
                    other_frames = [f for f in before.block.frames if f[:4] not in (b"TCON", b"COMM")]
                    other_chunks = [raw for key, _, raw in getattr(before, "parts", []) if key.lower() not in (b"id3 ", b"list")]
                    write_comment(path, "New comment")
                    self.assertEqual(read_comment(path), "New comment")
                    previous = write_genre(path, {"genre": "House"})
                    self.assertEqual(genre_values(path), (["House"], None))
                    if extension == ".wav":
                        self.assertEqual(AudioMetadata(path).info_value(b"IGNR"), b"House\0")
                    write_comment(path, "Later comment")
                    write_genre(path, {"restore": previous})
                    after = AudioMetadata(path)
                    self.assertEqual(read_comment(path), "Later comment")
                    self.assertEqual([f for f in after.block.frames if f[:4] == b"TCON"], [f for f in before.block.frames if f[:4] == b"TCON"])
                    self.assertEqual([f for f in after.block.frames if f[:4] not in (b"TCON", b"COMM")], other_frames)
                    if extension == ".mp3":
                        self.assertEqual(after.audio, before.audio)
                    else:
                        self.assertEqual([raw for key, _, raw in after.parts if key.lower() not in (b"id3 ", b"list")], other_chunks)
                    if extension == ".wav":
                        self.assertEqual(after.info_value(b"IGNR"), before.info_value(b"IGNR"))
                        self.assertEqual(after.info_value(b"INAM"), before.info_value(b"INAM"))
                    write_comment(path, "")
                    self.assertEqual(read_comment(path), "")

    def test_missing_metadata_and_wav_comments_use_info_not_id3(self):
        for extension in [".wav", ".aif", ".aiff"]:
            path = self.root / ("bare" + extension)
            fixture(path)
            self.assertEqual(read_comment(path), "")
            previous = write_genre(path, {"genre": "House"})
            write_comment(path, "Kept")
            write_genre(path, {"restore": previous})
            self.assertEqual(genre_values(path), ([], None))
            self.assertEqual(read_comment(path), "Kept")
        path = self.root / "legacy.wav"
        tags = rich_tags()
        tags.add(COMM(encoding=3, lang="eng", desc="", text=["ID3 only"]))
        fixture(path, tags)
        original_id3 = AudioMetadata(path).block.bytes()
        self.assertEqual(read_comment(path), "")
        write_comment(path, "Rekordbox comment")
        self.assertEqual(AudioMetadata(path).info_value(b"ICMT"), b"Rekordbox comment\0")
        self.assertEqual(AudioMetadata(path).block.bytes(), original_id3)

    def test_lint_all_formats_missing_mismatch_conflict_read_only(self):
        folder = self.root / "House"
        folder.mkdir()
        for extension in [".mp3", ".WAV", ".aif", ".AIFF"]:
            for state in ["good", "missing", "wrong"]:
                path = folder / (state + extension)
                fixture(path)
                if state != "missing":
                    write_genre(path, {"genre": "House" if state == "good" else "Breaks"})
        tags = ID3()
        tags.add(TCON(encoding=3, text=["House"]))
        fixture(folder / "id3-only.wav", tags)
        fixture(folder / "conflict.wav", tags, [(b"IGNR", b"Breaks\0")])
        tags["TCON"].text = ["Breaks"]
        fixture(folder / "false-success.wav", tags, [(b"IGNR", b"House\0")])
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()}
        result = lint_destinations(self.root, [{"directory": "House", "genre": "House"}])
        self.assertEqual(result["scanned"], 15)
        self.assertEqual(len(result["issues"]), 11)
        self.assertEqual(sum("Conflicting" in issue["problem"] for issue in result["issues"]), 3)
        for p, snapshot in before.items():
            self.assertEqual((p.read_bytes(), p.stat().st_mtime_ns), snapshot)

    def test_failures_leave_every_format_intact_and_clean_temporary_files(self):
        for extension in [".mp3", ".wav", ".aif", ".aiff"]:
            path = self.root / ("failure" + extension)
            fixture(path, rich_tags())
            before = path.read_bytes()
            for operation in [lambda: write_comment(path, "New"), lambda: write_genre(path, {"genre": "House"})]:
                with patch("comments.os.replace", side_effect=OSError("replace failure")):
                    with self.assertRaises(OSError):
                        operation()
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(list(self.root.glob(".audio-sorter-*")))
            with patch("comments.os.fsync", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    write_comment(path, "New")
            self.assertEqual(path.read_bytes(), before)
            link = self.root / ("link" + extension)
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                write_genre(link, {"genre": "House"})

    def test_rejects_ambiguous_wav_text_without_writes(self):
        path = self.root / "text.wav"
        fixture(path)
        before = path.read_bytes()
        for text in ["深い", "abc\0def"]:
            with self.assertRaises(ValueError):
                write_comment(path, text)
            with self.assertRaises(ValueError):
                write_genre(path, {"genre": text})
            self.assertEqual(path.read_bytes(), before)

    def test_corrupt_temporary_file_is_never_installed(self):
        for extension in [".mp3", ".wav", ".aif", ".aiff"]:
            path = self.root / ("verified" + extension)
            fixture(path)
            before = path.read_bytes()
            with patch("comments.os.fsync", side_effect=lambda fd: os.pwrite(fd, b"BAD", 0)):
                with self.assertRaises((OSError, ValueError)):
                    write_comment(path, "Never installed")
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(list(self.root.glob(".audio-sorter-*")))

    def test_flagged_and_truncated_id3_are_not_repaired(self):
        path = self.root / "unsupported.mp3"
        block = TagBlock()
        frame = block.encode(TCON(encoding=3, text=["House"]))
        variants = [b"ID3\x04\0\x80" + synchsafe(len(frame)) + frame,
                    b"ID3\x02\0\0" + synchsafe(0),
                    b"ID3\x04\0\0" + synchsafe(len(frame) + 1) + frame,
                    b"ID3\x04\0\0" + synchsafe(len(frame)) + frame[:8] + b"\0\x01" + frame[10:]]
        for data in variants:
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                write_genre(path, {"genre": "House"})
            self.assertEqual(path.read_bytes(), data)

    def test_malformed_and_duplicate_metadata_fail_closed_including_lint(self):
        folder = self.root / "House"
        folder.mkdir()
        path = folder / "bad.wav"
        fixture(path, rich_tags(), [(b"IGNR", b"House\0")])
        original = path.read_bytes()
        variants = [original[:-1], original + b"junk"]
        # Duplicate INFO, duplicate ID3, duplicate INFO fields, malformed INFO text.
        for extra in [chunk(b"LIST", b"INFO", "little"), chunk(b"id3 ", TagBlock().bytes(), "little")]:
            body = original[8:] + extra
            variants.append(b"RIFF" + len(body).to_bytes(4, "little") + body)
        for entries in [[(b"IGNR", b"House\0"), (b"IGNR", b"House\0")], [(b"IGNR", b"missing NUL")], [(b"IGNR", b"\xff\0")]]:
            fixture(path, info=entries)
            variants.append(path.read_bytes())
        for data in variants:
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                write_genre(path, {"genre": "House"})
            self.assertEqual(path.read_bytes(), data)
            report = lint_destinations(self.root, [{"directory": "House", "genre": "House"}])
            self.assertIn("Cannot read tags", report["issues"][0]["problem"])
            self.assertEqual(path.read_bytes(), data)
        # Raw duplicate TCON cannot be detected after Mutagen has collapsed it.
        for extension in [".mp3", ".aif", ".aiff", ".wav"]:
            path = self.root / ("duplicate" + extension)
            block = TagBlock()
            raw = block.encode(TCON(encoding=3, text=["House"]))
            duplicate = b"ID3\x04\0\0" + synchsafe(len(raw) * 2) + raw * 2
            fixture(path)
            if extension == ".mp3":
                data = duplicate + path.read_bytes()
            else:
                original = path.read_bytes()
                endian = "little" if extension == ".wav" else "big"
                body = original[8:] + chunk(b"id3 " if extension == ".wav" else b"ID3 ", duplicate, endian)
                data = original[:4] + len(body).to_bytes(4, endian) + body
            path.write_bytes(data)
            with self.assertRaisesRegex(ValueError, "duplicate ID3"):
                write_comment(path, "No repair")
            self.assertEqual(path.read_bytes(), data)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg unavailable")
    def test_real_audio_packets_unchanged_each_format(self):
        for extension in [".mp3", ".wav", ".aif", ".aiff"]:
            path = self.root / ("real" + extension)
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.1", "-y", str(path)], check=True)
            def payload():
                return subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a", "-c:a", "copy", "-f", "data", "-"])
            before = payload()
            write_comment(path, "Preview")
            previous = write_genre(path, {"genre": "House"})
            self.assertEqual(payload(), before)
            write_genre(path, {"restore": previous})
            self.assertEqual(payload(), before)
