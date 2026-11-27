"""Bar-edit splicing tests. Fixtures are created only in temporary folders."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

from mutagen.id3 import APIC, GEOB, ID3, PRIV, TBPM, TIT2, TLEN

from . import APP
from comments import AudioMetadata, chunk
from edit import Layout, bpm_text, cut_file, read_bpm, write_bpm

FADE = 8


def samples(frames, channels):
    # Distinct, slowly varying values so every frame is recognisable after splicing.
    return [((frame * 37 + channel * 1000) % 20000) - 10000 for frame in range(frames) for channel in range(channels)]


def container(path, chunks, aiff=False, form=b"AIFF"):
    endian = "big" if aiff else "little"
    body = (form if aiff else b"WAVE") + b"".join(chunk(key, payload, endian) for key, payload in chunks)
    path.write_bytes((b"FORM" if aiff else b"RIFF") + len(body).to_bytes(4, endian) + body)


def wav(path, frames=400, channels=2, extra=(), tags=None, fmt=None, pcm=None):
    if pcm is None:
        pcm = struct.pack("<%dh" % (frames * channels), *samples(frames, channels))
    fmt = fmt or struct.pack("<HHIIHH", 1, channels, 44100, 44100 * 2 * channels, 2 * channels, 16)
    chunks = [(b"fmt ", fmt), (b"data", pcm), *extra]
    if tags is not None:
        stream = io.BytesIO()
        tags.save(stream, v2_version=4, padding=lambda _: 0)
        chunks.append((b"id3 ", stream.getvalue()))
    container(path, chunks)


def aiff(path, frames=400, channels=2, extra=()):
    pcm = struct.pack(">%dh" % (frames * channels), *samples(frames, channels))
    comm = struct.pack(">hIh", channels, frames, 16) + bytes.fromhex("400eac44000000000000")
    container(path, [(b"COMM", comm), (b"SSND", b"\0" * 8 + pcm), *extra], aiff=True)


def frames_of(path):
    layout = Layout(AudioMetadata(path))
    return layout, layout.decode(layout.pcm())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def cut(self, source, segments, name=None, fade=FADE, **extra):
        output = self.root / (name or source.stem + " - Edit" + source.suffix)
        return cut_file(source, {"segments": segments, "fade": fade, "output": str(output), **extra}), output

    def assert_spliced(self, source, output, segments, channels=2):
        _, before = frames_of(source)
        _, after = frames_of(output)
        expected = []
        for start, end in segments:
            expected += before[start * channels:end * channels]
        self.assertEqual(len(after), len(expected))
        # Everything outside the fades before each splice is copied exactly.
        position = 0
        fades = []
        for i, (start, end) in enumerate(segments):
            if i and segments[i - 1][1] != start:
                fades.append(position)
            position += end - start
        for frame in range(len(after) // channels):
            if any(splice - FADE <= frame < splice for splice in fades):
                continue
            self.assertEqual(after[frame * channels:(frame + 1) * channels], expected[frame * channels:(frame + 1) * channels], frame)
        return before, after, fades

    def test_cut_and_duplicate_wav_are_exact_outside_the_fades(self):
        source = self.root / "track.wav"
        wav(source)
        original = digest(source)
        for segments in ([[0, 100], [200, 400]], [[0, 300], [200, 400]], [[0, 100], [200, 300], [200, 300], [300, 400]]):
            with self.subTest(segments=segments):
                _, output = self.cut(source, segments)
                before, after, fades = self.assert_spliced(source, output, segments)
                # The fade blends the outgoing audio with the audio just before the next segment.
                splice = fades[0]
                previous_end, next_start = segments[0][1], segments[1][0]
                for i in range(FADE):
                    gain = (i + 0.5) / FADE
                    for channel in range(2):
                        a = before[(previous_end - FADE + i) * 2 + channel]
                        b = before[(next_start - FADE + i) * 2 + channel]
                        self.assertEqual(after[(splice - FADE + i) * 2 + channel], round(a * (1 - gain) + b * gain))
                output.unlink()
        self.assertEqual(digest(source), original)

    def test_contiguous_segments_are_a_byte_identical_copy(self):
        source = self.root / "track.wav"
        wav(source)
        _, output = self.cut(source, [[0, 150], [150, 400]])
        self.assertEqual(output.read_bytes(), source.read_bytes())

    def test_fades_shrink_to_fit_near_the_start(self):
        source = self.root / "track.wav"
        wav(source)
        # Jumping back to frame 3 leaves only three frames of lead-in to fade from.
        _, output = self.cut(source, [[0, 100], [3, 50]])
        self.assertEqual(frames_of(output)[0].frames, 147)

    def test_sample_formats(self):
        frames, channels = 300, 2
        values = samples(frames, channels)
        cases = {
            "pcm8": (struct.pack("<HHIIHH", 1, channels, 8000, 8000 * channels, channels, 8), bytes((v // 100 + 128) for v in values)),
            "pcm24": (struct.pack("<HHIIHH", 1, channels, 8000, 8000 * 6, 6, 24), b"".join((v * 256).to_bytes(3, "little", signed=True) for v in values)),
            "float": (struct.pack("<HHIIHH", 3, channels, 8000, 8000 * 8, 8, 32), struct.pack("<%df" % len(values), *(v / 32768 for v in values))),
            "extensible": (struct.pack("<HHIIHHHHI", 0xFFFE, channels, 8000, 8000 * 8, 8, 32, 22, 24, 3) + struct.pack("<H", 1) + bytes(14),
                           b"".join((v * 65536).to_bytes(4, "little", signed=True) for v in values)),
        }
        for name, (fmt, pcm) in cases.items():
            with self.subTest(format=name):
                source = self.root / (name + ".wav")
                wav(source, fmt=fmt, pcm=pcm)
                _, output = self.cut(source, [[0, 100], [200, 300]])
                self.assert_spliced(source, output, [[0, 100], [200, 300]])
                layout, after = frames_of(output)
                if name == "extensible":
                    # 24 valid bits in a 32-bit container: the low byte stays clear in the fade too.
                    self.assertTrue(all(value % 256 == 0 for value in after))

    def test_aiff_updates_frame_count_and_drops_markers(self):
        source = self.root / "track.aiff"
        aiff(source, extra=[(b"MARK", b"\0\0"), (b"NAME", b"Kept")])
        result, output = self.cut(source, [[0, 100], [200, 400]])
        self.assert_spliced(source, output, [[0, 100], [200, 400]])
        metadata = AudioMetadata(output)
        comm = next(payload for key, payload, _ in metadata.parts if key == b"COMM")
        self.assertEqual(struct.unpack(">I", comm[2:6])[0], 300)
        self.assertEqual([key for key, _, _ in metadata.parts], [b"COMM", b"SSND", b"NAME"])
        self.assertEqual(result["dropped"], ["MARK"])

    def test_aifc_little_endian(self):
        source = self.root / "track.aif"
        frames, channels = 200, 2
        pcm = struct.pack("<%dh" % (frames * channels), *samples(frames, channels))
        comm = struct.pack(">hIh", channels, frames, 16) + bytes.fromhex("400eac44000000000000") + b"sowt" + b"\x00\x00"
        container(source, [(b"FVER", b"\xa2\x80\x51\x40"), (b"COMM", comm), (b"SSND", b"\0" * 8 + pcm)], aiff=True, form=b"AIFC")
        _, output = self.cut(source, [[0, 50], [100, 200]])
        self.assert_spliced(source, output, [[0, 50], [100, 200]])

    def test_timeline_metadata_is_dropped_and_the_rest_kept(self):
        tags = ID3()
        tags.add(TIT2(encoding=3, text=["Title"]))
        tags.add(TBPM(encoding=3, text=["124"]))
        tags.add(TLEN(encoding=3, text=["9070"]))
        tags.add(APIC(encoding=0, mime="image/jpeg", type=3, desc="cover", data=b"art"))
        tags.add(GEOB(encoding=0, mime="application/octet-stream", filename="", desc="Serato Markers2", data=b"cues"))
        tags.add(GEOB(encoding=0, mime="application/octet-stream", filename="", desc="Other", data=b"kept"))
        tags.add(PRIV(owner="TRAKTOR4", data=b"cues"))
        source = self.root / "track.wav"
        wav(source, tags=tags, extra=[(b"cue ", b"\0" * 4), (b"smpl", b"\0" * 36), (b"LIST", b"adtl"), (b"LIST", b"INFOINAM\x06\0\0\0Title\0"), (b"bext", b"kept")])
        result, output = self.cut(source, [[0, 100], [200, 400]])
        self.assertEqual(sorted(result["dropped"]), sorted(["cue", "smpl", "LIST adtl", "TLEN", "GEOB Serato Markers2", "PRIV TRAKTOR4"]))
        metadata = AudioMetadata(output)
        self.assertEqual([key for key, _, _ in metadata.parts], [b"fmt ", b"data", b"LIST", b"bext", b"id3 "])
        self.assertEqual(metadata.info_text(b"INAM"), "Title")
        kept = metadata.block.tags()
        self.assertEqual(sorted(kept.keys()), sorted(["TIT2", "TBPM", "APIC:cover", "GEOB:Other"]))

    def test_refuses_unsafe_requests_and_never_overwrites(self):
        source = self.root / "track.wav"
        wav(source)
        existing = self.root / "track - Edit.wav"
        existing.write_bytes(b"keep me")
        with self.assertRaises(FileExistsError):
            self.cut(source, [[0, 100], [200, 400]])
        self.assertEqual(existing.read_bytes(), b"keep me")
        (self.root / "other").mkdir()
        bad = [
            ([[0, 100], [200, 401]], "x.wav", {}),
            ([[100, 100]], "x.wav", {}),
            ([], "x.wav", {}),
            ([[0, 1.5]], "x.wav", {}),
            ([[0, 100]], "other/x.wav", {}),
            ([[0, 100]], "x.aiff", {}),
            ([[0, 100]], "track.wav", {}),
        ]
        for segments, name, extra in bad:
            with self.subTest(segments=segments, name=name), self.assertRaises(ValueError):
                self.cut(source, segments, name=name, **extra)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["other", "track - Edit.wav", "track.wav"])

    def test_cli_reports_errors_as_json(self):
        source = self.root / "track.wav"
        wav(source)
        script = str(APP / "comments.py")
        request = json.dumps({"segments": [[0, 100], [200, 400]], "fade": FADE, "output": str(self.root / "track - Edit.wav")})
        result = subprocess.run([sys.executable, script, "cut", str(source)], input=request, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["dropped"], [])
        again = subprocess.run([sys.executable, script, "cut", str(source)], input=request, capture_output=True, text=True)
        self.assertEqual(again.returncode, 1)
        self.assertIn("already exists", json.loads(again.stdout)["error"])

    def test_read_bpm(self):
        for text, expected in [("124", 124.0), ("123,5", 123.5), ("0", None), ("fast", None), (None, None)]:
            with self.subTest(text=text):
                tags = ID3()
                if text is not None:
                    tags.add(TBPM(encoding=3, text=[text]))
                source = self.root / "track.aiff"
                aiff(source)
                metadata = AudioMetadata(source)
                stream = io.BytesIO()
                tags.save(stream, v2_version=4, padding=lambda _: 0)
                body = metadata.original[8:] + chunk(b"ID3 ", stream.getvalue(), "big")
                source.write_bytes(b"FORM" + len(body).to_bytes(4, "big") + body)
                self.assertEqual(read_bpm(source), expected)

    def test_edits_carry_the_grid_bpm(self):
        self.assertEqual([bpm_text(v) for v in (124, 124.0, 123.5, 123.25, 99.999)], ["124", "124", "123.5", "123.25", "100"])
        for bad in (39, 301, "124", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                bpm_text(bad)
        # A WAV without any ID3 gets a new chunk holding only TBPM; the original is untouched.
        source = self.root / "plain.wav"
        wav(source)
        original = digest(source)
        _, output = self.cut(source, [[0, 100], [200, 400]], bpm=123.5)
        self.assertEqual(read_bpm(output), 123.5)
        self.assertEqual([key for key, _, _ in AudioMetadata(output).parts], [b"fmt ", b"data", b"id3 "])
        self.assertIsNone(read_bpm(source))
        self.assertEqual(digest(source), original)
        # An existing tag keeps its other frames and has its BPM replaced.
        tags = ID3()
        tags.add(TIT2(encoding=3, text=["Title"]))
        tags.add(TBPM(encoding=3, text=["120"]))
        tagged = self.root / "tagged.wav"
        wav(tagged, tags=tags)
        _, output = self.cut(tagged, [[0, 100], [200, 400]], bpm=124)
        kept = AudioMetadata(output).block.tags()
        self.assertEqual((kept["TBPM"].text, kept["TIT2"].text), (["124"], ["Title"]))
        self.assertEqual(AudioMetadata(output).block.version, 4)

    def test_write_bpm_in_place_changes_only_the_tag(self):
        for name in ("plain.wav", "plain.aiff"):
            with self.subTest(name=name):
                path = self.root / name
                (wav if name.endswith(".wav") else aiff)(path, extra=[(b"NAME", b"Kept")])
                before = Layout(AudioMetadata(path)).pcm()
                write_bpm(path, 126.5)
                self.assertEqual(read_bpm(path), 126.5)
                metadata = AudioMetadata(path)
                self.assertEqual(Layout(metadata).pcm(), before)
                self.assertIn(b"NAME", [key for key, _, _ in metadata.parts])
                write_bpm(path, 127)
                self.assertEqual(metadata.block.tags().get("TBPM").text, ["126.5"])
                self.assertEqual(AudioMetadata(path).block.tags()["TBPM"].text, ["127"])
                unchanged = digest(path)
                with self.assertRaises(ValueError):
                    write_bpm(path, 500)
                self.assertEqual(digest(path), unchanged)

    def test_write_bpm_cli(self):
        path = self.root / "plain.aiff"
        aiff(path)
        script = str(APP / "comments.py")
        result = subprocess.run([sys.executable, script, "write-bpm", str(path)], input='{"bpm": 124}', capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        read = subprocess.run([sys.executable, script, "read-bpm", str(path)], capture_output=True, text=True)
        self.assertEqual(json.loads(read.stdout), {"bpm": 124.0})

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg unavailable")
    def test_mp3_is_reencoded_from_the_decoded_audio_with_tags_carried_over(self):
        source = self.root / "track.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=220:duration=2:sample_rate=44100",
                        "-ac", "2", "-c:a", "libmp3lame", "-b:a", "192k", str(source)], check=True)
        tags = ID3()
        tags.add(TIT2(encoding=3, text=["Title"]))
        tags.add(TLEN(encoding=3, text=["2000"]))
        tags.add(APIC(encoding=0, mime="image/jpeg", type=3, desc="cover", data=b"art"))
        tags.save(source, v2_version=3)
        # The app decodes with AVFoundation; here FFmpeg stands in for it.
        decoded = self.root / "decoded.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-map", "0:a", "-c:a", "pcm_f32le", str(decoded)], capture_output=True, check=True)
        frames = Layout(AudioMetadata(decoded)).frames
        original = digest(source)
        result, output = self.cut(source, [[0, 22050], [44100, frames], [frames - 22050, frames]], decoded=str(decoded), bpm=124)
        self.assertEqual(result["dropped"], ["TLEN"])
        self.assertEqual(digest(source), original)
        metadata = AudioMetadata(output)
        self.assertEqual(metadata.block.version, 3)
        self.assertEqual(sorted(metadata.block.tags().keys()), ["APIC:cover", "TBPM", "TIT2"])
        self.assertEqual(read_bpm(output), 124)
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=bit_rate", "-of", "json", str(output)],
                               capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(probe.stdout)["streams"][0]["bit_rate"], "192000")
        # Gapless decoding returns exactly the edited length: 0.5 s + 1.0 s + 0.5 s.
        pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-map", "0:a", "-f", "f32le", "-"], capture_output=True, check=True).stdout
        self.assertEqual(len(pcm) // 8, 22050 + (frames - 44100) + 22050)
        with self.assertRaises(ValueError):
            self.cut(source, [[0, 100]], name="other.mp3")
        # An untagged MP3 gains a tag holding just the BPM.
        bare = self.root / "bare.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-map", "0:a", "-c:a", "copy", "-map_metadata", "-1",
                        "-id3v2_version", "0", "-write_id3v1", "0", str(bare)], capture_output=True, check=True)
        self.assertFalse(bare.read_bytes().startswith(b"ID3"))
        _, output = self.cut(bare, [[0, 22050], [44100, frames]], decoded=str(decoded), bpm=123.5)
        self.assertEqual(sorted(AudioMetadata(output).block.tags().keys()), ["TBPM"])
        self.assertEqual(read_bpm(output), 123.5)
        write_bpm(bare, 126)
        self.assertEqual(read_bpm(bare), 126)


if __name__ == "__main__":
    unittest.main()
