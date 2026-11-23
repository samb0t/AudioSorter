"""Write the rekordbox XML for every track whose grid was recorded in the editor. Invoked through comments.py.

The store (written by the app) maps absolute paths to a constant-tempo 4/4 grid and memory
cues, in seconds as AVFoundation decodes the file. Rekordbox reads the XML through
Preferences > Advanced > Database > rekordbox xml. Only memory cues are written (Num -1),
never hot cues. Files that no longer exist are left out; the store keeps their records.
"""
import json
import math
import os
from pathlib import Path
import tempfile
from urllib.parse import quote

from mutagen import File as MutagenFile
from mutagen.wave import WAVE

from comments import AudioMetadata, genre_values, read_comment, unsupported

KINDS = {".mp3": "MP3 File", ".wav": "WAV File", ".aif": "AIFF File", ".aiff": "AIFF File"}

# Samples added to every grid and cue time, by how the file starts, so that Rekordbox's
# positions match AVFoundation's (which the editor uses). This is the only place the
# correction lives. Measured with calibrate.py in Rekordbox 7.2.19 on 44.1 kHz files
# (see "Exporting to Rekordbox" in the README). Rekordbox ignores MP3 gapless headers:
# it plays every file 576 samples later than AVFoundation does without one, skips
# FFmpeg's header frame but not the encoder delay, and plays LAME's header frame as a
# frame of silence. MPEG-1 frames are 1152 samples at any rate.
OFFSETS = {
    "wav": 0,
    "aiff": 0,
    "mp3-lame": 2304,     # Measured: Xing/Info frame with a valid LAME tag (LAME, most stores).
    "mp3-lavc": 1152,     # Measured: the same, written by FFmpeg (the app's own MP3 edits).
    "mp3-bad-crc": 2304,  # Not measured: a LAME tag whose CRC does not match; assumed like LAME.
    "mp3-xing": 1152,     # Not measured: Xing/Info frame without a LAME tag; assumed like FFmpeg.
    "mp3-none": 576,      # Measured: no Xing/Info frame, so no gapless information at all.
}

MPEG_BITRATES = {  # kbps by (MPEG-1?, index), layer III only.
    True: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    False: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
MPEG_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def crc16(data):
    """CRC-16/ARC, as used for the LAME tag's own checksum."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def mp3_case(audio):
    """Classify how an MP3 stream (after any ID3v2 tag) starts, for OFFSETS."""
    pos = 0
    while pos + 4 <= len(audio):
        if audio[pos] == 0xFF and audio[pos + 1] & 0xE0 == 0xE0:
            version, layer = (audio[pos + 1] >> 3) & 3, (audio[pos + 1] >> 1) & 3
            bitrate, rate = audio[pos + 2] >> 4, (audio[pos + 2] >> 2) & 3
            if version != 1 and layer == 1 and 0 < bitrate < 15 and rate < 3:
                break
        pos += 1
    else:
        unsupported("no MPEG audio frame found")
    mpeg1, mono = version == 3, audio[pos + 3] >> 6 == 3
    side = (17 if mono else 32) if mpeg1 else (9 if mono else 17)
    xing = pos + 4 + side
    if audio[xing:xing + 4] not in (b"Xing", b"Info"):
        return "mp3-none"
    flags = int.from_bytes(audio[xing + 4:xing + 8], "big")
    lame = xing + 8 + 4 * bool(flags & 1) + 4 * bool(flags & 2) + 100 * bool(flags & 4) + 4 * bool(flags & 8)
    encoder = audio[lame:lame + 4]
    if encoder not in (b"LAME", b"Lavc", b"Lavf", b"L3.9", b"GOGO"):
        return "mp3-xing"
    if crc16(audio[pos:lame + 34]) != int.from_bytes(audio[lame + 34:lame + 36], "big"):
        return "mp3-bad-crc"
    return "mp3-lavc" if encoder in (b"Lavc", b"Lavf") else "mp3-lame"


def decode_case(path):
    suffix = Path(path).suffix.lower()
    if suffix == ".wav":
        return "wav"
    if suffix in (".aif", ".aiff"):
        return "aiff"
    return mp3_case(AudioMetadata(path).audio)


def first_beat(bpm, anchor):
    """The first beat at or after 0 s, and its place in the bar (Battito, 1–4).

    `anchor` is any downbeat; beats before it count backwards, so a pickup before bar 1
    gets Battito 2–4, and an anchor before 0 still puts beat 1 on its downbeats.
    """
    beat = 60 / bpm
    index = math.ceil(-anchor / beat - 1e-9)
    return max(0.0, anchor + index * beat), index % 4 + 1


def location(path):
    """file://localhost plus the percent-encoded absolute path (UTF-8, "/" kept)."""
    return "file://localhost" + quote(str(path), safe="/")


def attribute(value):
    """An XML attribute value with the five entities Pioneer's format list names."""
    text = "".join(c for c in str(value) if c in "\t\n\r" or ord(c) >= 0x20)
    text = "".join(c for c in text if not (0xD800 <= ord(c) <= 0xDFFF) and c not in "￾￿")
    for raw, entity in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;"), ("'", "&apos;")):
        text = text.replace(raw, entity)
    return '"' + text.replace("\n", "&#10;").replace("\r", "&#13;").replace("\t", "&#9;") + '"'


def seconds(value):
    return f"{max(0.0, value):.3f}"


def track_tags(path):
    """Name, Artist, Album, Genre and Comments as Rekordbox would read them; blanks if unreadable."""
    tags = {"Name": "", "Artist": "", "Album": "", "Genre": "", "Comments": ""}
    try:
        metadata = AudioMetadata(path)
        id3 = metadata.block.tags()
    except Exception:
        metadata = id3 = None
    for key, frame, info in (("Name", "TIT2", b"INAM"), ("Artist", "TPE1", b"IART"), ("Album", "TALB", b"IPRD")):
        if metadata is None:
            break
        try:
            # Rekordbox reads RIFF INFO for WAV; ID3 otherwise (and as the WAV fallback).
            tags[key] = metadata.info_text(info) if metadata.wav else ""
        except ValueError:
            pass
        if not tags[key] and id3.get(frame):
            tags[key] = " / ".join(str(text) for text in id3[frame].text)
    for key, read in (("Genre", lambda: next(iter(genre_values(path)[0]), "")), ("Comments", lambda: read_comment(path))):
        try:
            tags[key] = read()
        except Exception:
            pass
    if not tags["Name"].strip():
        tags["Name"] = Path(path).stem
    return tags


def audio_info(path, record):
    """TotalTime (whole seconds), SampleRate (Hz) and BitRate (kbps), else what the app recorded."""
    try:
        info = (WAVE(path) if Path(path).suffix.lower() == ".wav" else MutagenFile(path)).info
    except Exception:
        info = None
    length = getattr(info, "length", 0) or record.get("duration", 0)
    rate = getattr(info, "sample_rate", 0) or record.get("sampleRate", 0)
    return int(round(length)), int(rate), int(round((getattr(info, "bitrate", 0) or 0) / 1000))


def track_element(path, record):
    case = decode_case(path)
    total, rate, bitrate = audio_info(path, record)
    offset = OFFSETS[case] / (rate or 44100)
    bpm = round(record["bpm"], 2)  # Rekordbox keeps BPM to 0.01; the grid is exact at the downbeat.
    inizio, battito = first_beat(bpm, record["anchor"] + offset)
    tags = track_tags(path)
    attributes = {
        "TrackID": int(record["trackID"]),
        **{key: tags[key] for key in ("Name", "Artist", "Album", "Genre")},
        "Kind": KINDS[Path(path).suffix.lower()],
        "Size": os.stat(path).st_size,
        "TotalTime": total,
        "SampleRate": rate,
        "BitRate": bitrate,
        "AverageBpm": f"{bpm:.2f}",
        "Comments": tags["Comments"],
        "Location": location(path),
    }
    lines = ["    <TRACK " + " ".join(f"{key}={attribute(value)}" for key, value in attributes.items()) + ">"]
    lines.append(f'      <TEMPO Inizio="{seconds(inizio)}" Bpm="{bpm:.2f}" Metro="4/4" Battito="{battito}"/>')
    for cue in sorted(time + offset for time in record.get("cues", [])):
        if cue >= 0:
            # Num -1 is a memory cue; 0, 1, 2… would be hot cues A, B, C…, which are never written.
            lines.append(f'      <POSITION_MARK Name="" Type="0" Start="{seconds(cue)}" Num="-1"/>')
    lines.append("    </TRACK>")
    return lines, case


def build_xml(store, version="1.0"):
    """The XML text plus a report: written tracks, missing files and unreadable ones."""
    tracks, missing, failed, cases = [], [], [], {}
    for path, record in sorted(store.get("tracks", {}).items()):
        if not os.path.isfile(path):
            missing.append(path)
            continue
        try:
            lines, cases[path] = track_element(path, record)
            tracks.append(lines)
        except Exception as error:
            failed.append({"path": path, "error": str(error)})
    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<DJ_PLAYLISTS Version="1.0.0">',
        f'  <PRODUCT Name="Audio Sorter" Version={attribute(version)} Company=""/>',
        f'  <COLLECTION Entries="{len(tracks)}">',
        *[line for lines in tracks for line in lines],
        "  </COLLECTION>",
        "  <PLAYLISTS>",
        '    <NODE Type="0" Name="ROOT" Count="0"/>',
        "  </PLAYLISTS>",
        "</DJ_PLAYLISTS>",
        "",
    ]
    return "\n".join(body), {"tracks": len(tracks), "missing": missing, "failed": failed, "cases": cases}


def write_atomically(output, text):
    """Replace `output` only with a complete, flushed file; a failure leaves the old one."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".audio-sorter-", suffix=".xml", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def export(store_path, output):
    store_path = Path(store_path)
    store = json.loads(store_path.read_text(encoding="utf-8")) if store_path.exists() else {}
    text, report = build_xml(store)
    write_atomically(output, text)
    return {"output": str(output), **report}
