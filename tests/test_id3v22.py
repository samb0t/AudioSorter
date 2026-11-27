"""ID3v2.2 tags, as older iTunes wrote them: read as they are, upgraded to v2.3 on the first
write, and put back exactly by undo. Fixtures are created only in temporary folders."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from . import APP
from .test_formats import fixture
from comments import AudioMetadata, TagBlock, genre_values, read_comment, synchsafe, write_comment, write_genre
from edit import read_bpm, write_bpm


def frame(identifier, payload):
    return identifier + len(payload).to_bytes(3, "big") + payload


def itunes_tag(extra=b"", padding=300):
    """An ID3v2.2 tag like iTunes 12's: three-letter frames, its own COM frames, artwork."""
    body = b"".join([
        frame(b"TT2", b"\x00It Is Well"),
        frame(b"TP1", b"\x01" + "Ärtist".encode("utf-16")),
        frame(b"TCO", b"\x00Gospel"),
        frame(b"TBP", b"\x0072"),
        frame(b"COM", b"\x00engiTunNORM\x00 00000351 00000335"),
        frame(b"COM", b"\x00eng\x00Old comment"),
        frame(b"PIC", b"\x00PNG\x03cover\x00\x89PNG artwork"),
        frame(b"TEN", b"\x00iTunes 12.2.2.25"),
    ]) + extra + bytes(padding)
    return b"ID3\x02\x00\x00" + synchsafe(len(body)) + body


class RawTag:
    """Lets fixture() place an exact tag, which Mutagen can't write in v2.2."""
    def __init__(self, data):
        self.data = data

    def save(self, target, **_):
        if isinstance(target, Path):
            target.write_bytes(self.data + target.read_bytes())
        else:
            target.write(self.data)


class ID3v22Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def track(self, name, tag=None):
        path = self.root / name
        # WAV Genre lives in RIFF INFO; matching it keeps the fixture free of conflicts.
        fixture(path, RawTag(tag or itunes_tag()), [(b"IGNR", b"Gospel\0")] if name.endswith(".wav") else ())
        return path

    def test_reading_changes_nothing(self):
        for name in ("track.mp3", "track.aiff", "track.wav"):
            with self.subTest(name=name):
                path = self.track(name)
                before = path.read_bytes()
                self.assertEqual(read_comment(path), "" if name.endswith(".wav") else "Old comment")
                self.assertEqual(genre_values(path), (["Gospel"], None))
                self.assertEqual(read_bpm(path), 72)
                self.assertEqual(path.read_bytes(), before)

    def test_first_write_upgrades_without_loss(self):
        for name in ("track.mp3", "track.aiff"):
            with self.subTest(name=name):
                path = self.track(name)
                old = AudioMetadata(path)
                self.assertTrue(write_comment(path, "New comment"))
                new = AudioMetadata(path)
                self.assertEqual(new.block.version, 3)
                self.assertIsNone(new.block.upgraded_from)
                tags = new.block.tags()
                self.assertEqual(str(tags["TIT2"]), "It Is Well")
                self.assertEqual(str(tags["TPE1"]), "Ärtist")
                self.assertEqual(str(tags["TCON"]), "Gospel")
                self.assertEqual(str(tags["TBPM"]), "72")
                self.assertEqual(str(tags["TENC"]), "iTunes 12.2.2.25")
                self.assertEqual(tags["COMM:iTunNORM:eng"].text, [" 00000351 00000335"])
                self.assertEqual((tags["APIC:cover"].mime, tags["APIC:cover"].data), ("image/png", b"\x89PNG artwork"))
                self.assertEqual(read_comment(path), "New comment")
                if name.endswith(".mp3"):
                    self.assertEqual(new.audio, old.audio)
                else:
                    self.assertEqual([p for p in new.parts if p[0] != b"ID3 "], [p for p in old.parts if p[0] != b"ID3 "])
                self.assertFalse(write_comment(path, "Again"))  # Already v2.3.

    def test_itunes_playback_comments_are_not_the_comment(self):
        # iTunes files often have only iTunNORM/iTunPGAP-style COMM frames.
        frames = frame(b"COM", b"\x00engiTunPGAP\x000") + frame(b"COM", b"\x00engiTunNORM\x00 00000351")
        path = self.track("itunes.mp3", b"ID3\x02\x00\x00" + synchsafe(len(frames)) + frames)
        self.assertEqual(read_comment(path), "")
        write_comment(path, "Mine")
        tags = AudioMetadata(path).block.tags()
        self.assertEqual(read_comment(path), "Mine")
        self.assertEqual((tags["COMM:iTunPGAP:eng"].text, tags["COMM:iTunNORM:eng"].text), (["0"], [" 00000351"]))

    def test_conversion_keeps_the_tag_size(self):
        # The old padding absorbs v2.3's longer frame headers, so nothing after the tag moves.
        tag = itunes_tag()
        converted = TagBlock(tag)
        self.assertEqual((converted.version, len(converted.bytes())), (3, len(tag)))
        self.assertEqual(converted.upgraded_from, tag)
        # Without room to spare, the tag grows rather than losing anything.
        self.assertGreater(len(TagBlock(itunes_tag(padding=0)).bytes()), len(itunes_tag(padding=0)))

    def test_bpm_write_upgrades(self):
        path = self.track("track.mp3")
        self.assertTrue(write_bpm(path, 124))
        self.assertEqual(read_bpm(path), 124)
        self.assertEqual(AudioMetadata(path).block.version, 3)

    def test_genre_undo_puts_the_original_tag_back(self):
        for name in ("track.mp3", "track.aiff", "track.wav"):
            with self.subTest(name=name):
                path = self.track(name)
                before = path.read_bytes()
                previous = write_genre(path, {"genre": "House"})
                self.assertIn("upgradedFrom", previous)
                self.assertEqual(genre_values(path), (["House"], None))
                self.assertEqual(AudioMetadata(path).block.version, 3)
                write_genre(path, {"restore": previous})
                self.assertEqual(path.read_bytes(), before)

    def test_undo_after_a_later_edit_keeps_the_edit(self):
        path = self.track("track.mp3")
        previous = write_genre(path, {"genre": "House"})
        write_comment(path, "Edited after sorting")
        write_genre(path, {"restore": previous})
        self.assertEqual(genre_values(path), (["Gospel"], None))
        self.assertEqual(read_comment(path), "Edited after sorting")
        self.assertEqual(AudioMetadata(path).block.version, 3)

    def test_tags_that_cant_convert_or_are_flagged_are_refused(self):
        cases = {
            "can't be converted": itunes_tag(extra=frame(b"XYZ", b"abcd")),
            "compression": b"ID3\x02\x00\x40" + itunes_tag()[6:],
        }
        for message, tag in cases.items():
            with self.subTest(message=message):
                path = self.track("refused.mp3", tag)
                before = path.read_bytes()
                for operation in (lambda: read_comment(path), lambda: write_comment(path, "x"),
                                  lambda: write_genre(path, {"genre": "House"})):
                    with self.assertRaisesRegex(ValueError, message):
                        operation()
                self.assertEqual(path.read_bytes(), before)
                path.unlink()

    def test_header_problems_are_named(self):
        for flags, message in ((0x80, "unsynchronization"), (0x40, "extended header"), (0x20, "experimental")):
            with self.subTest(message=message):
                path = self.root / "flagged.mp3"
                path.write_bytes(b"ID3\x03\x00" + bytes([flags]) + synchsafe(0) + b"audio")
                with self.assertRaisesRegex(ValueError, message):
                    read_comment(path)

    def test_helper_reports_the_upgrade(self):
        path = self.track("track.mp3")
        helper = [sys.executable, str(APP / "comments.py")]
        first = subprocess.run(helper + ["write", str(path)], input="Hi", capture_output=True, text=True)
        self.assertEqual(json.loads(first.stdout), {"ok": True, "upgradedID3": True}, first.stderr)
        again = subprocess.run(helper + ["write", str(path)], input="Hi again", capture_output=True, text=True)
        self.assertEqual(json.loads(again.stdout), {"ok": True, "upgradedID3": False}, again.stderr)


if __name__ == "__main__":
    unittest.main()
