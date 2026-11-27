"""Check that Rekordbox puts grids and memory cues where the editor did, per file type.

    .venv/bin/python tools/calibrate.py [folder]            # fixtures + calibration.xml
    .venv/bin/python tools/calibrate.py [folder] --update   # the same tracks, changed, to test re-import
    .venv/bin/python tools/calibrate.py [folder] --verify   # new "Verify" tracks, offsets applied

Writes click tracks (WAV, AIFF, MP3 from LAME, MP3 from FFmpeg, MP3 without a gapless
header) to a scratch folder, never the music library, and an XML that uses the app's own
exporter. Each track is 120 BPM: a pickup click at 0.3 s (beat 4), then bar 1 at 0.8 s,
with downbeats louder. Bar 1 has a memory cue on its click; bars 3–9 have one cue each,
deliberately moved off the click by -26, -13, -6.5, 0, +6.5, +13 and +26 ms. If Rekordbox
decodes a file d ms later than AVFoundation, the grid sits d ms early and the cue moved by
+d sits exactly on its click: d is the correction for rekordbox.OFFSETS.

--verify writes separately named tracks (new TrackIDs, so Rekordbox treats them as new)
whose cues on bars 3–7 are off by only -2, -1, 0, +1 and +2 ms. With the offsets in
rekordbox.OFFSETS applied by the exporter, every grid line and the bar 5 cue should sit
on their clicks.
"""
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

from mutagen.id3 import COMM, ID3, TCON, TIT2, TPE1

# Uses the app's own helpers, exporter included.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import rekordbox
from comments import chunk
from edit import ffmpeg

RATE = 44100
BPM = 120
BEAT = 60 / BPM
DOWNBEAT = 0.8
PICKUP = DOWNBEAT - BEAT
LENGTH = 20
PROBES = [-26, -13, -6.5, 0, 6.5, 13, 26]  # ms, on the downbeats of bars 3–9.
FINE = [-2, -1, 0, 1, 2]  # ms, on bars 3–7 when verifying.
NAME = "Café & Co #1 100%"  # Exercises Location encoding: space, &, #, % and an accent.

MEASURE = r'''
import AVFoundation
let expected = CommandLine.arguments[1].split(separator: ",").map { Double($0)! }
var result: [String: [Double]] = [:]
for path in CommandLine.arguments.dropFirst(2) {
    let file = try AVAudioFile(forReading: URL(fileURLWithPath: path))
    let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(file.length))!
    try file.read(into: buffer)
    let samples = buffer.floatChannelData![0], rate = file.processingFormat.sampleRate
    // Each click's attack: the first sample above half the loudest one within 60 ms.
    result[path] = expected.map { time in
        let start = max(0, Int((time - 0.06) * rate)), end = min(Int(buffer.frameLength), Int((time + 0.06) * rate))
        let peak = (start..<end).map { abs(samples[$0]) }.max() ?? 0
        let onset = (start..<end).first { abs(samples[$0]) >= peak / 2 } ?? start
        return Double(onset) / rate
    }
}
print(String(decoding: try JSONSerialization.data(withJSONObject: result), as: UTF8.self))
'''


def click_times():
    """Every beat from the pickup to the end, and whether it is a downbeat."""
    times, beat = [], PICKUP
    while beat < LENGTH - 0.5:
        times.append((beat, round((beat - DOWNBEAT) / BEAT) % 4 == 0))
        beat += BEAT
    return times


def write_clicks(path):
    samples = [0.0] * (RATE * LENGTH)
    for time, downbeat in click_times():
        start = round(time * RATE)
        # Starts at full height on its first sample, so the attack is unambiguous.
        for i in range(int(0.004 * RATE)):
            samples[start + i] += (0.9 if downbeat else 0.4) * math.cos(2 * math.pi * 3000 * i / RATE) * math.exp(-i / (0.0008 * RATE))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(b"".join(struct.pack("<hh", int(s * 32767), int(s * 32767)) for s in samples))


def tag(path, title):
    tags = ID3()
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text="Audio Sorter"))
    tags.add(TCON(encoding=3, text="Calibration"))
    tags.add(COMM(encoding=3, lang="eng", desc="", text="Delete after calibrating"))
    tags.save(path, v2_version=3)


def make_fixtures(folder, prefix):
    source = folder / ".clicks.wav"
    write_clicks(source)
    run = lambda *arguments: subprocess.run([ffmpeg(), "-v", "error", "-nostdin", "-y", *map(str, arguments)], check=True)
    fixtures = {}
    path = folder / f"{prefix} 1 WAV – {NAME}.wav"
    shutil.copy(source, path)
    data = path.read_bytes()  # Title in RIFF INFO, which Rekordbox reads for WAV.
    info = b"INFO" + b"".join(chunk(key, value + b"\0", "little") for key, value in
                              ((b"INAM", f"{prefix} 1 - WAV".encode()), (b"IART", b"Audio Sorter"), (b"IGNR", b"Calibration")))
    body = data[8:] + chunk(b"LIST", info, "little")
    path.write_bytes(b"RIFF" + len(body).to_bytes(4, "little") + body)
    fixtures[path] = f"{prefix} 1 - WAV"
    path = folder / f"{prefix} 2 AIFF – {NAME}.aiff"
    run("-i", source, "-c:a", "pcm_s16be", "-write_id3v2", "1", "-metadata", f"title={prefix} 2 - AIFF",
        "-metadata", "artist=Audio Sorter", "-metadata", "genre=Calibration", path)
    fixtures[path] = f"{prefix} 2 - AIFF"
    lame = shutil.which("lame", path=":".join(["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]))
    if lame:
        path = folder / f"{prefix} 3 MP3 LAME – {NAME}.mp3"
        subprocess.run([lame, "--quiet", "-b", "320", str(source), str(path)], check=True)
        fixtures[path] = f"{prefix} 3 - MP3 (LAME)"
    else:
        print("lame is not installed (brew install lame); skipping the LAME-encoded MP3.")
    path = folder / f"{prefix} 4 MP3 FFmpeg – {NAME}.mp3"
    run("-i", source, "-c:a", "libmp3lame", "-b:a", "320k", path)
    fixtures[path] = f"{prefix} 4 - MP3 (FFmpeg, like saved edits)"
    path = folder / f"{prefix} 5 MP3 no header – {NAME}.mp3"
    run("-i", source, "-c:a", "libmp3lame", "-b:a", "320k", "-write_xing", "0", path)
    fixtures[path] = f"{prefix} 5 - MP3 (no gapless header)"
    for path, title in fixtures.items():
        if path.suffix == ".mp3":
            tag(path, title)
    source.unlink()
    return list(fixtures)


def measure(paths):
    """Where AVFoundation (the editor's decoder) puts each click."""
    expected = [time for time, _ in click_times()]
    with tempfile.TemporaryDirectory() as work:
        script = Path(work) / "measure.swift"
        script.write_text(MEASURE)
        result = subprocess.run(["swift", str(script), ",".join(map(str, expected)), *map(str, paths)], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError("Cannot measure with AVFoundation: " + result.stderr)
        return {Path(path): dict(zip(expected, onsets)) for path, onsets in json.loads(result.stdout).items()}


def build(folder, update, verify):
    prefix = "Verify" if verify else "Calibration"
    probes = FINE if verify else PROBES
    paths = sorted(folder.glob(prefix + " *"))
    paths = [path for path in paths if path.suffix in (".wav", ".aiff", ".mp3")]
    if not paths or not update:
        for old in paths:
            old.unlink()
        paths = make_fixtures(folder, prefix)
    onsets = measure(paths)
    tracks, lines = {}, []
    for number, path in enumerate(paths, 1):
        clicks = onsets[path]
        shift = clicks[DOWNBEAT] - DOWNBEAT
        bar = lambda n: DOWNBEAT + (n - 1) * 4 * BEAT
        cues = [clicks[DOWNBEAT]] + [clicks[bar(3 + i)] + probe / 1000 for i, probe in enumerate(probes)]
        anchor = clicks[DOWNBEAT]
        if update:
            # Re-import test: grid one beat later (so the pickup becomes bar 1) and a cue on bar 2.
            anchor -= BEAT
            cues.append(clicks[bar(2)])
        key = str(path.resolve())
        tracks[key] = {"bpm": BPM, "anchor": anchor, "cues": cues, "trackID": number + (10 if verify else 0), "duration": LENGTH, "sampleRate": RATE}
        spread = max(abs(clicks[t] - t - shift) for t in clicks) * 1000
        lines.append((path.name, shift * 1000, spread))
    text, report = rekordbox.build_xml({"tracks": tracks})
    output = folder / ("verify.xml" if verify else "calibration.xml")
    rekordbox.write_atomically(output, text)
    return output, lines, report


def main():
    arguments = [a for a in sys.argv[1:] if not a.startswith("--")]
    update, verify = "--update" in sys.argv, "--verify" in sys.argv
    folder = Path(arguments[0] if arguments else "/tmp/Audio Sorter Calibration").expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    output, lines, report = build(folder, update, verify)
    print(f"Wrote {output}")
    print(f"{'File':60} {'Case':12} {'AVFoundation vs source':>24}")
    for name, shift, spread in lines:
        case = report["cases"][str(folder / name)]
        print(f"{name:60} {case:12} {shift:+10.2f} ms (±{spread:.2f})")
    if report["failed"]:
        print("Not exported:", report["failed"])
    if verify:
        print("Offsets applied (samples):", {case: rekordbox.OFFSETS[case] for case in sorted(set(report["cases"].values()))})
        print("Cue offsets on bars 3–7:", ", ".join(f"{p:+g} ms" for p in FINE))
    else:
        print("Cue offsets on bars 3–9:", ", ".join(f"{p:+g} ms" for p in PROBES))


if __name__ == "__main__":
    main()
