import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from mutagen.id3 import COMM, ID3, TIT2, TCON

from comments import read_comment, write_comment, write_genre


class CommentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "track.mp3"
        self.path.write_bytes(b"test audio payload" * 100)

    def test_add_unicode_edit_and_clear(self):
        self.assertEqual(read_comment(self.path), "")
        write_comment(self.path, "深い bass 🎵\nSecond line 123")
        self.assertEqual(read_comment(self.path), "深い bass 🎵\nSecond line 123")
        write_comment(self.path, "Updated")
        self.assertEqual(read_comment(self.path), "Updated")
        write_comment(self.path, "")
        self.assertEqual(read_comment(self.path), "")

    def test_preserves_other_frames_and_v23(self):
        tags = ID3()
        tags.add(TIT2(encoding=3, text=["Original title"]))
        tags.add(COMM(encoding=3, lang="eng", desc="", text=["Old comment"]))
        tags.add(COMM(encoding=3, lang="eng", desc="iTunNORM", text=["normalization data"]))
        tags.save(self.path, v2_version=3)
        write_comment(self.path, "New comment")
        result = ID3(self.path)
        self.assertEqual(result.version[1], 3)
        self.assertEqual(str(result["TIT2"]), "Original title")
        self.assertEqual(result["COMM:iTunNORM:eng"].text, ["normalization data"])
        self.assertEqual(read_comment(self.path), "New comment")

    def test_preserves_named_comment_identity(self):
        tags = ID3()
        tags.add(COMM(encoding=3, lang="deu", desc="Description", text=["Old"]))
        tags.save(self.path)
        write_comment(self.path, "New")
        self.assertEqual(ID3(self.path)["COMM:Description:deu"].text, ["New"])

    def test_rejects_symlink(self):
        link = self.path.with_name("link.mp3")
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            write_comment(link, "Do not write")
        self.assertTrue(link.is_symlink())

    def test_failed_write_leaves_original_intact(self):
        from unittest.mock import patch
        before = self.path.read_bytes()
        with patch("comments.ID3.save", side_effect=OSError("Disk error")):
            with self.assertRaises(OSError):
                write_comment(self.path, "New")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob(".audio-sorter-*")), [])

    def test_genre_set_and_restore_multiple_values(self):
        tags = ID3()
        tags.add(TCON(encoding=1, text=["Old genre", "Another genre"]))
        tags.add(TIT2(encoding=1, text=["Original title"]))
        tags.save(self.path, v2_version=3)
        persisted_text = list(ID3(self.path)["TCON"].text)
        previous = write_genre(self.path, {"genre": "深い House"})
        self.assertEqual(previous["text"], persisted_text)
        self.assertEqual(ID3(self.path)["TCON"].text, ["深い House"])
        self.assertEqual(ID3(self.path).version[1], 3)
        write_comment(self.path, "Updated comment")
        self.assertEqual(ID3(self.path)["TCON"].text, ["深い House"])
        write_genre(self.path, {"restore": previous})
        result = ID3(self.path)
        self.assertEqual(result["TCON"].text, previous["text"])
        self.assertEqual(int(result["TCON"].encoding), previous["encoding"])
        self.assertEqual(str(result["TIT2"]), "Original title")
        self.assertEqual(read_comment(self.path), "Updated comment")

    def test_genre_undo_removes_previously_absent_frame(self):
        previous = write_genre(self.path, {"genre": "House"})
        self.assertIsNone(previous)
        self.assertEqual(ID3(self.path).version[1], 4)
        write_genre(self.path, {"restore": previous})
        self.assertNotIn("TCON", ID3(self.path))

    def test_genre_rejects_empty_and_symlink(self):
        before = self.path.read_bytes()
        for value in ["", "  ", None, 123]:
            with self.assertRaises(ValueError):
                write_genre(self.path, {"genre": value})
        self.assertEqual(self.path.read_bytes(), before)
        link = self.path.with_name("link.mp3")
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            write_genre(link, {"genre": "House"})
        self.assertTrue(link.is_symlink())

    def test_failed_genre_write_leaves_original_intact(self):
        from unittest.mock import patch
        before = self.path.read_bytes()
        with patch("comments.ID3.save", side_effect=OSError("Disk error")):
            with self.assertRaises(OSError):
                write_genre(self.path, {"genre": "House"})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob(".audio-sorter-*")), [])

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg unavailable")
    def test_real_mp3_audio_unchanged(self):
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-y", str(self.path)], check=True)

        def audio_hash():
            audio = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(self.path), "-map", "0:a", "-c:a", "copy", "-f", "data", "-"])
            return hashlib.sha256(audio).hexdigest()

        before = audio_hash()
        write_comment(self.path, "Previewed at 30%; keep this one 🎵")
        previous = write_genre(self.path, {"genre": "House"})
        self.assertEqual(audio_hash(), before)
        write_genre(self.path, {"restore": previous})
        self.assertEqual(audio_hash(), before)
        self.assertEqual(read_comment(self.path), "Previewed at 30%; keep this one 🎵")


if __name__ == "__main__":
    unittest.main()
