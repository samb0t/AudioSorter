"""Grid detection tests. The fitting runs on synthetic beat lists; the model test needs beat_this."""
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import wave

import numpy as np

from . import APP, ROOT
from comments import chunk
from detect import fit_grid, onset_strength, read_wav, refine_phase

MODEL = ROOT / ".build" / "beat_this" / "final0.ckpt"
FRAME = 0.02  # beat_this reports times on a 50 fps frame grid.


def beat_list(bpm, downbeat, duration, jitter=0.0, quantize=True, seed=1):
    """Beats and downbeats as beat_this would report them for a steady track."""
    rng = np.random.default_rng(seed)
    period = 60 / bpm
    first = int(np.ceil(-downbeat / period))
    times = downbeat + period * np.arange(first, int((duration - downbeat) / period) + 1)
    times = times[(times >= 0) & (times < duration)]
    numbers = np.round((times - downbeat) / period).astype(int)
    times = times + rng.normal(0, jitter, len(times)) if jitter else times
    if quantize:
        times = np.round(times / FRAME) * FRAME
    return times, times[numbers % 4 == 0]


def float_wav(path, samples, rate):
    data = np.asarray(samples, dtype="<f4").tobytes()
    fmt = struct.pack("<HHIIHH", 3, 1, rate, rate * 4, 4, 32)
    body = b"WAVE" + chunk(b"fmt ", fmt, "little") + chunk(b"data", data, "little")
    path.write_bytes(b"RIFF" + len(body).to_bytes(4, "little") + body)


def on_beat(grid, bpm, downbeat):
    """How far the grid's anchor is from the nearest true downbeat (seconds)."""
    bar = 240 / bpm
    offset = (grid["anchor"] - downbeat) / bar
    return abs(offset - round(offset)) * bar


class FitTests(unittest.TestCase):
    def test_steady_track_with_frame_quantisation(self):
        for bpm in (124, 123.5, 126.37, 174):
            with self.subTest(bpm=bpm):
                beats, downbeats = beat_list(bpm, 0.517, 360, jitter=0.004)
                grid = fit_grid(beats, downbeats, 360)
                self.assertAlmostEqual(60 / grid["bpm"], 60 / bpm, delta=2e-5)
                self.assertLess(on_beat(grid, bpm, 0.517), 0.004)
                self.assertGreater(grid["confidence"], 0.95)
                self.assertEqual(grid["downbeatConfidence"], 1.0)
                self.assertLess(grid["drift"], 0.01)
                self.assertEqual(grid["rounded"], float(bpm * 2).is_integer())

    def test_round_bpm_is_preferred_only_when_it_fits(self):
        # 0.002 BPM is under 3 ms over five minutes, well inside the frame noise.
        beats, downbeats = beat_list(124.002, 0.3, 300)
        self.assertEqual(fit_grid(beats, downbeats, 300)["bpm"], 124.0)
        beats, downbeats = beat_list(124.2, 0.3, 300)
        grid = fit_grid(beats, downbeats, 300)
        self.assertFalse(grid["rounded"])
        self.assertAlmostEqual(grid["bpm"], 124.2, delta=0.01)

    def test_anchor_is_a_downbeat_near_the_middle(self):
        beats, downbeats = beat_list(120, 0.8, 200)
        grid = fit_grid(beats, downbeats, 200)
        self.assertLess(abs(grid["anchor"] - 100.8), 1.0)
        self.assertLess(on_beat(grid, 120, 0.8), 0.002)

    def test_missed_extra_and_misplaced_beats(self):
        beats, downbeats = beat_list(125, 1.1, 300)
        keep = np.ones(len(beats), dtype=bool)
        keep[200:216] = False  # A breakdown with no beats detected.
        keep[::37] = False     # Scattered misses.
        noisy = np.sort(np.concatenate([beats[keep], [10.13, 55.37, 120.01]]))  # Off-beat detections.
        # A few bars' downbeats on the wrong beat don't outvote the rest.
        wrong = downbeats.copy()
        wrong[5:9] += 60 / 125
        grid = fit_grid(noisy, wrong, 300)
        self.assertEqual(grid["bpm"], 125.0)
        self.assertLess(on_beat(grid, 125, 1.1), 0.002)
        self.assertGreater(grid["downbeatConfidence"], 0.9)

    def test_pickup_before_bar_one(self):
        # The first detected beat is the pickup (beat 4); the downbeat is the next one.
        beats, downbeats = beat_list(128, 0.6, 120)
        self.assertLess(beats[0], 0.6)
        grid = fit_grid(beats, downbeats, 120)
        self.assertLess(on_beat(grid, 128, 0.6), 0.002)

    def test_drift_is_reported(self):
        # Creeping up 0.3 BPM still fits, with a warning; 2 BPM has no constant grid at all.
        beats = np.cumsum(np.linspace(60 / 124, 60 / 124.3, 700))
        grid = fit_grid(beats, beats[::4], beats[-1] + 1)
        self.assertGreater(grid["drift"], 0.03)
        beats = np.cumsum(np.linspace(60 / 124, 60 / 126, 700))
        with self.assertRaises(ValueError):
            fit_grid(beats, beats[::4], beats[-1] + 1)

    def test_too_few_or_unsteady_beats_are_refused(self):
        with self.assertRaises(ValueError):
            fit_grid([0.5, 1.0, 1.5], [0.5], 10)
        rng = np.random.default_rng(3)
        with self.assertRaises(ValueError):
            fit_grid(np.sort(rng.uniform(0, 100, 200)), [], 100)


class PhaseTests(unittest.TestCase):
    def test_grid_moves_onto_attacks(self):
        rate, bpm, downbeat = 44100, 124, 0.4321
        samples = np.zeros(rate * 30)
        period = 60 / bpm
        for time in np.arange(downbeat, 29.5, period):
            start = int(round(time * rate))
            decay = np.exp(-np.arange(2000) / 300)
            samples[start:start + 2000] += 0.8 * np.sin(2 * np.pi * 60 * np.arange(2000) / rate) * decay
        samples += np.random.default_rng(0).normal(0, 0.001, len(samples))
        rise, step = onset_strength(samples, rate)
        self.assertAlmostEqual(step, 0.001, delta=1e-5)
        # Start 18 ms late, as frame-quantised beats can be.
        refined = refine_phase(samples, rate, {"bpm": bpm, "anchor": downbeat + 0.018})
        self.assertLess(on_beat(refined, bpm, downbeat), 0.0015)
        self.assertAlmostEqual(refined["shift"], -0.018, delta=0.0015)

    def test_float_wav_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decoded.wav"
            samples = np.sin(np.arange(4410) / 10)
            float_wav(path, samples, 44100)
            read, rate = read_wav(path)
            self.assertEqual(rate, 44100)
            np.testing.assert_allclose(read, samples, atol=1e-6)
            with wave.open(str(Path(directory) / "int.wav"), "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(22050)
                output.writeframes(struct.pack("<4h", 16384, -16384, 8192, 8192))
            read, rate = read_wav(Path(directory) / "int.wav")
            self.assertEqual(rate, 22050)
            np.testing.assert_allclose(read, [0.0, 0.25])


def house_loop(path, rate=44100, bpm=124, downbeat=0.37):
    """Kick on every beat, clap on 2 and 4, a chord change each bar, written as a float WAV."""
    period = 60 / bpm
    samples = np.zeros(int(rate * 40))
    t = np.arange(int(rate * 0.25)) / rate
    kick = np.sin(2 * np.pi * (50 + 100 * np.exp(-t * 30)) * t) * np.exp(-t * 12)
    clap = np.random.default_rng(1).normal(0, 0.3, len(t)) * np.exp(-t * 40)
    for number, time in enumerate(np.arange(downbeat, 39, period)):
        start = int(round(time * rate))
        end = min(len(samples), start + len(t))
        samples[start:end] += kick[:end - start]
        if number % 4 in (1, 3):
            samples[start:end] += clap[:end - start]
        if number % 4 == 0:
            chord = np.arange(int(rate * period * 4)) / rate
            root = [55, 65.4, 49, 73.4][(number // 4) % 4]
            pad = 0.1 * sum(np.sin(2 * np.pi * root * ratio * chord) for ratio in (1, 1.26, 1.5))
            samples[start:start + len(pad)] += pad[:len(samples) - start]
    float_wav(path, samples / np.max(np.abs(samples)), rate)


def assert_house_loop_grid(test, grid, bpm=124, downbeat=0.37):
    test.assertEqual(grid["bpm"], float(bpm))
    # The beat phase must be right to within a couple of milliseconds; the downbeat
    # vote on synthetic audio is not asserted.
    period = 60 / bpm
    beat_offset = (grid["anchor"] - downbeat) / period
    test.assertLess(abs(beat_offset - round(beat_offset)) * period, 0.003)


@unittest.skipUnless(importlib.util.find_spec("beat_this") and MODEL.is_file(), "beat_this not installed (zsh build.sh --detect)")
class ModelTests(unittest.TestCase):
    def test_detects_a_synthetic_house_loop(self):
        """Through the helper, as the editor runs it."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "loop.wav"
            house_loop(path)
            helper = APP / "comments.py"
            result = subprocess.run([sys.executable, str(helper), "detect", str(path)],
                                    input=json.dumps({"model": str(MODEL)}), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        assert_house_loop_grid(self, json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
