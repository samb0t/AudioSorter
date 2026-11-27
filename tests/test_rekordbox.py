"""Rekordbox XML export tests. Fixtures are created only in temporary folders."""
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import unquote
import wave
import xml.etree.ElementTree as ElementTree

from mutagen.id3 import ID3, TIT2, TPE1, TCON

from . import APP
import rekordbox
from comments import chunk

AWKWARD = "Café & Co #1 100% 'Dub' (Edit)"


def tone(path, seconds=3, rate=44100):
    frames = b"".join(struct.pack("<hh", s, s) for s in (int(8000 * math.sin(i / 20)) for i in range(int(rate * seconds))))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(frames)


def add_info(path, fields):
    """Append a RIFF LIST/INFO chunk to a WAV written by the wave module."""
    data = path.read_bytes()
    info = b"INFO" + b"".join(chunk(key, value.encode("ascii") + b"\0", "little") for key, value in fields.items())
    body = data[8:] + chunk(b"LIST", info, "little")
    path.write_bytes(b"RIFF" + len(body).to_bytes(4, "little") + body)


def ffmpeg(*arguments):
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *map(str, arguments)], check=True)


def parse(text):
    return ElementTree.fromstring(text.encode("utf-8"))


class GridMathTests(unittest.TestCase):
    def test_first_beat_and_battito(self):
        # 120 BPM: beats every 0.5 s.
        cases = [
            (0.8, (0.3, 4)),     # A pickup beat before bar 1.
            (0.5, (0.0, 4)),     # The beat at 0 s is the pickup's last beat.
            (2.0, (0.0, 1)),     # Downbeat at 0 s, anchored four beats later.
            (-0.1, (0.4, 2)),    # Anchor before 0: the first beat is the bar's second.
            (-0.6, (0.4, 3)),
            (100.3, (0.3, 1)),   # A downbeat set deep in the track.
            (-1e-12, (0.0, 1)),  # Rounding noise never yields a negative Inizio.
        ]
        for anchor, (inizio, battito) in cases:
            with self.subTest(anchor=anchor):
                start, beat = rekordbox.first_beat(120, anchor)
                self.assertAlmostEqual(start, inizio, places=9)
                self.assertEqual(beat, battito)
                self.assertGreaterEqual(start, 0)
                self.assertLess(start, 0.5)

    def test_battito_puts_beat_one_on_downbeats(self):
        for bpm in (90, 123.45, 174):
            for anchor in (-3.21, -0.01, 0, 0.02, 0.7, 1.9, 61.234):
                start, battito = rekordbox.first_beat(bpm, anchor)
                bar = 240 / bpm
                first_downbeat = start + ((5 - battito) % 4) * 60 / bpm
                offset = (first_downbeat - anchor) / bar
                self.assertAlmostEqual(offset, round(offset), places=6, msg=(bpm, anchor))


class EncodingTests(unittest.TestCase):
    def test_location_percent_encodes_awkward_paths(self):
        path = "/Users/dj/Música/AC#DC & Co 100% (Edit) 'x' [1].mp3"
        url = rekordbox.location(path)
        self.assertEqual(url, "file://localhost/Users/dj/M%C3%BAsica/AC%23DC%20%26%20Co%20100%25%20%28Edit%29%20%27x%27%20%5B1%5D.mp3")
        self.assertEqual(unquote(url[len("file://localhost"):]), path)

    def test_attribute_escaping(self):
        self.assertEqual(rekordbox.attribute("A & B <C> \"D\" 'E'"), '"A &amp; B &lt;C&gt; &quot;D&quot; &apos;E&apos;"')
        self.assertEqual(rekordbox.attribute("one\ntwo\x01\x1f"), '"one&#10;two"')
        self.assertEqual(parse(f"<X a={rekordbox.attribute(AWKWARD + ' é ü')}/>").get("a"), AWKWARD + " é ü")


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg unavailable")
class ExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()

    def tearDown(self):
        self.directory.cleanup()

    def fixtures(self):
        wav_path = self.root / f"{AWKWARD}.wav"
        tone(wav_path)
        add_info(wav_path, {b"INAM": "Riff Title", b"IART": "Riff Artist", b"IGNR": "House", b"ICMT": "energy 7"})
        source = self.root / "source.wav"
        tone(source)
        lavc = self.root / "ffmpeg é.mp3"
        ffmpeg("-i", source, "-c:a", "libmp3lame", "-b:a", "192k", lavc)
        bare = self.root / "no header.mp3"
        ffmpeg("-i", source, "-c:a", "libmp3lame", "-b:a", "192k", "-write_xing", "0", bare)
        aiff = self.root / "tone.aiff"
        ffmpeg("-i", source, "-c:a", "pcm_s16be", "-write_id3v2", "1", "-metadata", "title=Aiff Title", aiff)
        tags = ID3()
        tags.add(TIT2(encoding=3, text="Mp3 Title & More"))
        tags.add(TPE1(encoding=3, text="Ärtist"))
        tags.add(TCON(encoding=3, text="Tech House"))
        tags.save(lavc)
        return wav_path, lavc, bare, aiff

    def store(self, paths):
        records = {}
        for number, path in enumerate(paths, 1):
            records[str(path)] = {"bpm": 124.004, "anchor": 0.8 + number / 100, "cues": [2.5, 0.9 + number / 100], "trackID": number, "duration": 3}
        return {"version": 1, "nextTrackID": len(paths) + 1, "tracks": records}

    def test_xml_structure_metadata_and_cues(self):
        wav_path, lavc, bare, aiff = self.fixtures()
        missing = self.root / "renamed in Finder.wav"
        store = self.store([wav_path, lavc, bare, aiff, missing])
        text, report = rekordbox.build_xml(store)
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<DJ_PLAYLISTS Version="1.0.0">'))
        root = parse(text)
        self.assertEqual(root.tag, "DJ_PLAYLISTS")
        self.assertEqual([child.tag for child in root], ["PRODUCT", "COLLECTION", "PLAYLISTS"])
        self.assertEqual(root.find("PRODUCT").attrib, {"Name": "Audio Sorter", "Version": "1.0", "Company": ""})
        collection = root.find("COLLECTION")
        tracks = {unquote(track.get("Location")[len("file://localhost"):]): track for track in collection}
        self.assertEqual(collection.get("Entries"), "4")
        self.assertEqual(set(tracks), {str(wav_path), str(lavc), str(bare), str(aiff)})
        self.assertEqual(report["missing"], [str(missing)])
        self.assertEqual(report["failed"], [])
        self.assertEqual(report["cases"], {str(wav_path): "wav", str(lavc): "mp3-lavc", str(bare): "mp3-none", str(aiff): "aiff"})
        # Records without a day (from before playlists) share one playlist.
        rootNode = root.find("PLAYLISTS/NODE")
        self.assertEqual(rootNode.attrib, {"Type": "0", "Name": "ROOT", "Count": "1"})
        self.assertEqual([node.get("Name") for node in rootNode.find("NODE")], ["Earlier"])
        self.assertEqual(sorted(track.get("Key") for track in rootNode.iter("TRACK")), ["1", "2", "3", "4"])
        wav_track = tracks[str(wav_path)]
        self.assertEqual({key: wav_track.get(key) for key in ("TrackID", "Name", "Artist", "Genre", "Comments", "Kind", "SampleRate", "TotalTime", "AverageBpm")},
                         {"TrackID": "1", "Name": "Riff Title", "Artist": "Riff Artist", "Genre": "House", "Comments": "energy 7",
                          "Kind": "WAV File", "SampleRate": "44100", "TotalTime": "3", "AverageBpm": "124.00"})
        self.assertEqual(wav_track.get("Size"), str(wav_path.stat().st_size))
        self.assertEqual(wav_track.get("BitRate"), "1411")
        mp3 = tracks[str(lavc)]
        self.assertEqual((mp3.get("Name"), mp3.get("Artist"), mp3.get("Genre"), mp3.get("Kind"), mp3.get("BitRate")),
                         ("Mp3 Title & More", "Ärtist", "Tech House", "MP3 File", "192"))
        self.assertEqual(tracks[str(bare)].get("Name"), "no header")  # No tags: the file name.
        self.assertEqual((tracks[str(aiff)].get("Name"), tracks[str(aiff)].get("Kind")), ("Aiff Title", "AIFF File"))
        # One TEMPO at the first beat, BPM to two decimals, and Battito 4 for the pickup beat.
        tempo = wav_track.findall("TEMPO")
        beat = 60 / 124
        self.assertEqual(len(tempo), 1)
        self.assertEqual(tempo[0].attrib, {"Inizio": f"{0.81 - beat:.3f}", "Bpm": "124.00", "Metro": "4/4", "Battito": "4"})
        # Memory cues only, sorted, in seconds; never hot cues.
        marks = [mark.attrib for mark in wav_track.findall("POSITION_MARK")]
        self.assertEqual(marks, [{"Name": "", "Type": "0", "Start": "0.910", "Num": "-1"}, {"Name": "", "Type": "0", "Start": "2.500", "Num": "-1"}])
        self.assertTrue(all(mark.get("Num") == "-1" for mark in root.iter("POSITION_MARK")))

    def test_offsets_apply_per_case_and_drop_cues_before_zero(self):
        _, lavc, bare, _ = self.fixtures()
        store = {"tracks": {str(bare): {"bpm": 120, "anchor": 0.01, "cues": [0.005, 1.0], "trackID": 7},
                            str(lavc): {"bpm": 120, "anchor": 0.01, "cues": [0.005, 1.0], "trackID": 8}}}
        # Offsets are in samples: a negative one can push the first beat and early cues before 0.
        with mock.patch.dict(rekordbox.OFFSETS, {"mp3-none": -529}):
            root = parse(rekordbox.build_xml(store)[0])
        by_id = {track.get("TrackID"): track for track in root.iter("TRACK")}
        self.assertEqual(by_id["7"].find("TEMPO").get("Inizio"), "0.498")
        self.assertEqual(by_id["7"].find("TEMPO").get("Battito"), "2")
        self.assertEqual([mark.get("Start") for mark in by_id["7"].iter("POSITION_MARK")], ["0.988"])
        # The calibrated FFmpeg offset: one 1152-sample frame, 26.1 ms at 44.1 kHz.
        self.assertEqual(by_id["8"].find("TEMPO").get("Inizio"), "0.036")
        self.assertEqual([mark.get("Start") for mark in by_id["8"].iter("POSITION_MARK")], ["0.031", "1.026"])
        self.assertEqual({key: rekordbox.OFFSETS[key] for key in ("wav", "aiff", "mp3-lame", "mp3-lavc", "mp3-none")},
                         {"wav": 0, "aiff": 0, "mp3-lame": 2304, "mp3-lavc": 1152, "mp3-none": 576})

    def test_mp3_start_classification(self):
        _, lavc, bare, _ = self.fixtures()
        self.assertEqual(rekordbox.decode_case(lavc), "mp3-lavc")
        self.assertEqual(rekordbox.decode_case(bare), "mp3-none")
        if shutil.which("lame"):
            lame = self.root / "lame.mp3"
            subprocess.run(["lame", "--quiet", "-b", "320", str(self.root / "source.wav"), str(lame)], check=True)
            self.assertEqual(rekordbox.decode_case(lame), "mp3-lame")
        data = bytearray(lavc.read_bytes())
        audio_start = len(data) - len(rekordbox.AudioMetadata(lavc).audio)
        lame_tag = data.index(b"Lavc", audio_start)
        damaged = self.root / "damaged.mp3"
        data[lame_tag + 21] ^= 0xFF  # Encoder delay bits, covered by the tag's CRC.
        damaged.write_bytes(data)
        self.assertEqual(rekordbox.decode_case(damaged), "mp3-bad-crc")
        data[lame_tag:lame_tag + 4] = b"\0\0\0\0"
        damaged.write_bytes(data)
        self.assertEqual(rekordbox.decode_case(damaged), "mp3-xing")

    def test_sorted_tracks_without_grid_and_daily_playlists(self):
        wav_path, lavc, bare, aiff = self.fixtures()
        store = {"tracks": {
            str(wav_path): {"cues": [], "trackID": 1, "day": "2026-11-20"},
            str(lavc): {"bpm": 124, "anchor": 0.5, "cues": [1.0], "trackID": 2, "day": "2026-11-22"},
            str(bare): {"cues": [], "trackID": 3, "day": "2026-11-22"},
            str(aiff): {"cues": [], "trackID": 4, "day": "2026-11-22", "trashed": True},
            str(self.root / "gone.wav"): {"cues": [], "trackID": 5, "day": "2026-11-21"},
        }}
        root = parse(rekordbox.build_xml(store)[0])
        tracks = {track.get("TrackID"): track for track in root.find("COLLECTION")}
        # A sorted track without a grid has no TEMPO or cues, so Rekordbox analyses it.
        self.assertEqual(list(tracks["1"]), [])
        self.assertEqual((tracks["1"].get("AverageBpm"), tracks["1"].get("Name")), ("0.00", "Riff Title"))
        self.assertEqual([child.tag for child in tracks["2"]], ["TEMPO", "POSITION_MARK"])
        # One playlist per day, newest first; missing files are in neither collection nor playlists.
        folder = root.find("PLAYLISTS/NODE/NODE")
        self.assertEqual(folder.attrib, {"Type": "0", "Name": "Audio Sorter", "Count": "2"})
        days = [(node.get("Name"), node.get("Type"), node.get("KeyType"), node.get("Entries"), [t.get("Key") for t in node])
                for node in folder]
        self.assertEqual(days, [("2026-11-22", "1", "0", "3", ["2", "3", "4"]), ("2026-11-20", "1", "0", "1", ["1"])])
        self.assertEqual(set(tracks), {"1", "2", "3", "4"})
        # Every playlist key is a TrackID in the collection.
        self.assertTrue({t.get("Key") for t in folder.iter("TRACK")} <= set(tracks))

    def test_unreadable_track_is_reported_not_fatal(self):
        wav_path, *_ = self.fixtures()
        broken = self.root / "broken.mp3"
        broken.write_bytes(b"not audio at all")
        store = {"tracks": {str(broken): {"bpm": 120, "anchor": 0, "cues": [], "trackID": 1},
                            str(wav_path): {"bpm": 120, "anchor": 0, "cues": [], "trackID": 2}}}
        text, report = rekordbox.build_xml(store)
        self.assertEqual(parse(text).find("COLLECTION").get("Entries"), "1")
        self.assertEqual([failure["path"] for failure in report["failed"]], [str(broken)])

    def test_export_through_helper_writes_atomically(self):
        wav_path, *_ = self.fixtures()
        store_path = self.root / "grids.json"
        store_path.write_text(json.dumps(self.store([wav_path])))
        output = self.root / "Rekordbox out" / "rekordbox.xml"
        helper = APP / "comments.py"
        result = subprocess.run([sys.executable, str(helper), "rekordbox-xml", str(store_path)],
                                input=json.dumps({"output": str(output)}), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report["output"], report["tracks"], report["missing"]), (str(output), 1, []))
        first = output.read_text(encoding="utf-8")
        self.assertEqual(parse(first).find("COLLECTION").get("Entries"), "1")
        # A failure while writing or replacing leaves the previous XML and no temporary files.
        for target in ("os.replace", "os.fsync"):
            with self.subTest(failing=target), mock.patch(target, side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    rekordbox.write_atomically(output, "<broken")
            self.assertEqual(output.read_text(encoding="utf-8"), first)
            self.assertEqual(os.listdir(output.parent), ["rekordbox.xml"])
        # An empty or absent store still writes a valid, empty collection.
        rekordbox.export(self.root / "absent.json", output)
        self.assertEqual(parse(output.read_text(encoding="utf-8")).find("COLLECTION").get("Entries"), "0")


if __name__ == "__main__":
    unittest.main()
