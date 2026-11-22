"""Write a rearranged copy of a track (bars cut or repeated). Invoked through comments.py.

WAV/AIFF: PCM bytes outside the short crossfade before each splice are copied
verbatim, and unrelated chunks are preserved. MP3: the caller decodes with the
same decoder the editor used (so frame numbers match what was shown), this module
splices that WAV, re-encodes once with FFmpeg, and carries the ID3 tag across.
Metadata that points at positions in the old audio is dropped, not left stale.
The source file is never modified, and an existing output is never overwritten.
"""
import io
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

from mutagen.id3 import ID3, TBPM
from mutagen.mp3 import MP3, BitrateMode

from comments import AudioMetadata, TagBlock, chunk, synchsafe, unsupported

# Cue points, loops, beat counts and timing tables all describe the old timeline.
POSITIONAL_CHUNKS = {b"cue ", b"smpl", b"acid", b"plst", b"MARK", b"INST"}
POSITIONAL_FRAMES = {"TLEN", "MLLT", "ETCO", "SYTC", "SYLT", "ASPI", "POSS"}
MP3_BITRATES = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)


class Layout:
    """Where the PCM frames live in a container and how samples are encoded."""

    def __init__(self, metadata):
        parts = metadata.parts
        if metadata.wav:
            fmt = next(payload for key, payload, _ in parts if key == b"fmt ")
            if len(fmt) < 16:
                unsupported("short fmt chunk")
            tag, self.channels = struct.unpack("<HH", fmt[:4])
            self.block, self.bits = struct.unpack("<HH", fmt[12:16])
            if tag == 0xFFFE:
                if len(fmt) < 40:
                    unsupported("short WAVE_FORMAT_EXTENSIBLE fmt chunk")
                valid = struct.unpack("<H", fmt[18:20])[0]
                self.bits = valid or self.bits
                tag = struct.unpack("<H", fmt[24:26])[0]
            self.kind = {1: "int", 3: "float"}.get(tag) or unsupported("compressed WAV audio")
            self.endian = "little"
            self.audio_key = b"data"
            index = next(i for i, (key, _, _) in enumerate(parts) if key == b"data")
            payload = parts[index][1]
            self.prefix = 0
            if not self.channels or not self.block or len(payload) % self.block:
                unsupported("WAV data length is not a whole number of frames")
            self.frames = len(payload) // self.block
        else:
            comm = next(payload for key, payload, _ in parts if key == b"COMM")
            if len(comm) < 18:
                unsupported("short COMM chunk")
            self.channels, self.frames, self.bits = struct.unpack(">hIh", comm[:8])
            compression = comm[18:22] if metadata.original[8:12] == b"AIFC" else b"NONE"
            self.kind, self.endian = {
                b"NONE": ("int", "big"), b"twos": ("int", "big"), b"sowt": ("int", "little"),
                b"fl32": ("float", "big"), b"FL32": ("float", "big"),
                b"fl64": ("float", "big"), b"FL64": ("float", "big"),
            }.get(compression) or unsupported("compressed AIFC audio")
            width = (self.bits + 7) // 8 if self.kind == "int" else (8 if compression in (b"fl64", b"FL64") else 4)
            if self.channels <= 0:
                unsupported("AIFF channel count")
            self.block = self.channels * width
            self.audio_key = b"SSND"
            index = next(i for i, (key, _, _) in enumerate(parts) if key == b"SSND")
            payload = parts[index][1]
            if len(payload) < 8:
                unsupported("short SSND chunk")
            self.prefix = 8 + struct.unpack(">I", payload[:4])[0]
            if self.prefix + self.frames * self.block > len(payload):
                unsupported("SSND shorter than COMM frame count")
        self.index = index
        self.payload = payload
        self.width = self.block // self.channels
        if self.block != self.channels * self.width or (self.kind == "int" and self.width not in (1, 2, 3, 4)) \
                or (self.kind == "float" and self.width not in (4, 8)):
            unsupported("PCM sample size")
        # Samples narrower than their container are left-justified; keep unused low bits clear.
        self.step = 1 << (8 * self.width - self.bits) if self.kind == "int" and 0 < self.bits < 8 * self.width else 1
        self.unsigned = metadata.wav and self.kind == "int" and self.width == 1

    def pcm(self):
        return self.payload[self.prefix:self.prefix + self.frames * self.block]

    def decode(self, data):
        if self.kind == "float":
            return list(struct.unpack(("<" if self.endian == "little" else ">") + ("f" if self.width == 4 else "d") * (len(data) // self.width), data))
        offset = 128 if self.unsigned else 0
        return [int.from_bytes(data[i:i + self.width], self.endian, signed=not self.unsigned) - offset
                for i in range(0, len(data), self.width)]

    def encode(self, values):
        if self.kind == "float":
            return struct.pack(("<" if self.endian == "little" else ">") + ("f" if self.width == 4 else "d") * len(values), *values)
        limit = 1 << (8 * self.width - 1)
        offset = 128 if self.unsigned else 0
        output = bytearray()
        for value in values:
            value = round(value / self.step) * self.step
            value = min(max(value, -limit), limit - self.step)
            output += int(value + offset).to_bytes(self.width, self.endian, signed=not self.unsigned)
        return bytes(output)


def crossfade(layout, outgoing, incoming):
    """Equal-gain fade from the outgoing frames into the incoming ones."""
    a, b = layout.decode(outgoing), layout.decode(incoming)
    frames = len(a) // layout.channels
    mixed = []
    for frame in range(frames):
        gain = (frame + 0.5) / frames
        for channel in range(layout.channels):
            i = frame * layout.channels + channel
            mixed.append(a[i] * (1 - gain) + b[i] * gain)
    return layout.encode(mixed)


def without_positional_frames(block):
    """Return (block, dropped names); untouched blocks are returned as-is."""
    kept, dropped = [], []
    for frame in block.frames:
        name = frame[:4].decode("ascii")
        drop = name in POSITIONAL_FRAMES
        if name in ("GEOB", "PRIV"):
            decoded = ID3(fileobj=io.BytesIO(b"ID3" + bytes([block.version, 0, 0]) + synchsafe(len(frame)) + frame), translate=False)
            for value in decoded.values():
                # Serato and Traktor store cues, loops and beat grids for the old timeline here.
                if (name == "GEOB" and value.desc.startswith("Serato ")) or (name == "PRIV" and value.owner == "TRAKTOR4"):
                    drop = True
                    name = f"{name} {value.desc if name == 'GEOB' else value.owner}"
        (dropped if drop else kept).append(name if drop else frame)
    if not dropped:
        return block, []
    filtered = TagBlock()
    filtered.version, filtered.frames, filtered.padding = block.version, kept, block.padding
    return filtered, dropped


def check_segments(frames, segments, fade):
    if not isinstance(fade, int) or fade < 0 or not isinstance(segments, list) or not segments:
        raise ValueError("An edit needs at least one segment and a whole-frame fade")
    for segment in segments:
        if not (isinstance(segment, list) and len(segment) == 2 and all(isinstance(v, int) for v in segment)):
            raise ValueError("Segments must be [start, end] whole frames")
        if not 0 <= segment[0] < segment[1] <= frames:
            raise ValueError(f"Segment {segment[0]}–{segment[1]} is outside the track's {frames} frames")


def splice_pcm(layout, pcm, segments, fade):
    """Concatenate source frame ranges, crossfading wherever playback jumps.

    The fade sits just before each splice: the end of what came before fades into the
    audio leading up to the next segment, so the downbeat after the splice is untouched.
    """
    block = layout.block
    output = bytearray()
    previous = None
    for start, end in segments:
        if previous is not None and previous[1] != start:
            width = min(fade, start, previous[1] - previous[0])
            if width:
                tail = bytes(output[-width * block:])
                output[-width * block:] = crossfade(layout, tail, pcm[(start - width) * block:start * block])
        output += pcm[start * block:end * block]
        previous = (start, end)
    return bytes(output)


def bpm_text(bpm):
    """124 or 123.5: the grid's tempo, without trailing zeros."""
    if not isinstance(bpm, (int, float)) or not 40 <= bpm <= 300:
        raise ValueError("BPM must be between 40 and 300")
    return f"{bpm:.2f}".rstrip("0").rstrip(".")


def with_bpm(block, bpm):
    """A copy of the tag with TBPM set; other frames stay byte-identical."""
    updated = TagBlock()
    updated.version, updated.frames, updated.padding = block.version, list(block.frames), block.padding
    updated.replace(b"TBPM", updated.encode(TBPM(encoding=0, text=[bpm_text(bpm)])))
    return updated


def splice_container(metadata, segments, fade, bpm=None):
    """Return (new file bytes, dropped metadata names, new frame count) for WAV/AIFF."""
    layout = Layout(metadata)
    check_segments(layout.frames, segments, fade)
    block = layout.block
    spliced = splice_pcm(layout, layout.pcm(), segments, fade)
    remaining = len(spliced) // block
    dropped, body = [], []
    for i, (key, payload, raw) in enumerate(metadata.parts):
        if key in POSITIONAL_CHUNKS or (metadata.wav and key == b"LIST" and payload[:4] == b"adtl"):
            dropped.append(key.decode("latin-1").strip() + (" adtl" if key == b"LIST" else ""))
            continue
        if i == layout.index:
            raw = chunk(key, payload[:layout.prefix] + spliced + payload[layout.prefix + layout.frames * block:], metadata.endian)
        elif key == b"COMM":
            raw = chunk(key, payload[:2] + struct.pack(">I", remaining) + payload[6:], "big")
        elif key == b"fact" and len(payload) >= 4:
            raw = chunk(key, struct.pack("<I", remaining) + payload[4:], "little")
        elif key.lower() == b"id3 ":
            filtered, names = without_positional_frames(TagBlock(payload))
            dropped += names
            if bpm is not None:
                filtered = with_bpm(filtered, bpm)
            if names or bpm is not None:
                raw = chunk(key, filtered.bytes(), metadata.endian)
        body.append(raw)
    if bpm is not None and metadata.id3_index is None:
        body.append(chunk(b"id3 " if metadata.wav else b"ID3 ", with_bpm(TagBlock(), bpm).bytes(), metadata.endian))
    data = metadata.original[8:12] + b"".join(body)
    return metadata.original[:4] + len(data).to_bytes(4, metadata.endian) + data, dropped, remaining


def ffmpeg():
    # Apps launched from Finder get a minimal PATH; Homebrew installs elsewhere.
    path = shutil.which("ffmpeg", path=os.pathsep.join([os.environ.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"]))
    if path is None:
        raise RuntimeError("MP3 edits need FFmpeg to re-encode. Install it with: brew install ffmpeg")
    return path


def mp3_quality(source):
    info = MP3(source).info
    if info.bitrate_mode == BitrateMode.VBR:
        return ["-q:a", "0"]
    kbps = min(MP3_BITRATES, key=lambda rate: abs(rate * 1000 - info.bitrate))
    return ["-b:a", f"{kbps}k"]


def splice_mp3(metadata, decoded, segments, fade, work, bpm=None):
    """Splice the AVFoundation decode, re-encode once, then reattach the original tag."""
    spliced, _, _ = splice_container(AudioMetadata(decoded), segments, fade)
    cut = Path(work) / "cut.wav"
    cut.write_bytes(spliced)
    encoded = Path(work) / "cut.mp3"
    result = subprocess.run([ffmpeg(), "-v", "error", "-nostdin", "-i", str(cut), "-map", "0:a", "-c:a", "libmp3lame",
                             *mp3_quality(metadata.path), "-map_metadata", "-1", "-id3v2_version", "0",
                             "-write_id3v1", "0", "-f", "mp3", str(encoded)], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("FFmpeg could not encode the edit: " + (result.stderr.strip() or "unknown error"))
    audio = encoded.read_bytes()
    if not audio or audio.startswith(b"ID3"):
        raise RuntimeError("FFmpeg produced unexpected MP3 output")
    block, dropped = without_positional_frames(metadata.block)
    if bpm is not None:
        block = with_bpm(block, bpm)
    trailer = metadata.audio[-128:] if len(metadata.audio) >= 128 and metadata.audio[-128:-125] == b"TAG" else b""
    return (block.bytes() if metadata.id3_present or bpm is not None else b"") + audio + trailer, dropped


def write_new(output, data, source):
    """Write next to the source without ever replacing an existing file."""
    fd, temporary = tempfile.mkstemp(prefix=".audio-sorter-", suffix=output.suffix, dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, source.stat().st_mode & 0o777)
        if AudioMetadata(temporary).original != data:
            raise OSError("Temporary file verification failed")
        os.link(temporary, output)  # Fails if output exists, unlike rename.
    finally:
        os.unlink(temporary)


def cut_file(source, request):
    source = Path(source)
    output = Path(request["output"])
    segments, fade, bpm = request["segments"], request.get("fade", 0), request.get("bpm")
    if output.parent != source.parent or output.suffix.lower() != source.suffix.lower() or output.name == source.name:
        raise ValueError("The edit must be a new file beside the original with the same extension")
    if os.path.lexists(output):
        raise FileExistsError(f"{output.name} already exists; it was not replaced")
    metadata = AudioMetadata(source)
    if metadata.container:
        data, dropped, remaining = splice_container(metadata, segments, fade, bpm)
    else:
        decoded = request.get("decoded")
        if not decoded:
            raise ValueError("MP3 edits need the decoded audio")
        with tempfile.TemporaryDirectory(prefix="audio-sorter-") as work:
            data, dropped = splice_mp3(metadata, decoded, segments, fade, work, bpm)
        remaining = None
    write_new(output, data, source)
    if metadata.container and Layout(AudioMetadata(output)).frames != remaining:
        raise OSError(f"{output.name} was written but its length could not be verified")
    return {"output": str(output), "dropped": dropped}


def read_bpm(path):
    """BPM from the ID3 tag (Beatport and most stores set it), or None."""
    frame = AudioMetadata(path).block.tags().get("TBPM")
    for text in frame.text if frame else []:
        try:
            bpm = float(str(text).strip().replace(",", "."))
        except ValueError:
            continue
        if 40 <= bpm <= 300:
            return bpm
    return None


def write_bpm(path, bpm):
    """Set TBPM on an existing file in place, with the same lossless, verified writer as comments.

    WAV files get an ID3 chunk if they have none; RIFF INFO has no BPM field.
    """
    metadata = AudioMetadata(path)
    metadata.block = with_bpm(metadata.block, bpm)
    metadata.save(edit_id3=True)
