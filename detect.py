"""Detect a constant-tempo 4/4 grid with beat_this. Invoked through comments.py.

The app decodes the track with AVFoundation into a float WAV first, so every time here is
on the editor's timeline. beat_this (Foscarin, Schlüter, Widmer, ISMIR 2024) finds beats
and downbeats at 50 frames per second, so single beats are only good to about 20 ms and
follow any tempo drift. One straight line through all of them gives the tempo; the
downbeats vote for which beat is 1; and a final search lines the grid up with the attacks
in the audio. See docs/beat-this-plan.md.
"""
import math
import struct

import numpy as np

from comments import AudioMetadata, unsupported
from edit import Layout

OUTLIER = 0.04       # s: beats further than this from the fitted line don't shape it.
ON_GRID = 0.02       # s: a beat this close to the grid counts towards the confidence.
ROUND_SLACK = 0.001  # s: a round BPM is preferred if its RMS error is within this of the best fit.
SEARCH = 0.025       # s: how far the phase search moves the grid to find the attacks.
DRIFT_WINDOW = 16    # beats: residuals are smoothed over this many before measuring drift.


def read_wav(path):
    """Mono float samples and the sample rate of a PCM WAV (the app writes 32-bit float)."""
    metadata = AudioMetadata(path)
    if not metadata.wav:
        unsupported("detection reads WAV only")
    layout = Layout(metadata)
    fmt = next(payload for key, payload, _ in metadata.parts if key == b"fmt ")
    rate = struct.unpack("<I", fmt[4:8])[0]
    data = layout.pcm()
    if layout.kind == "float":
        samples = np.frombuffer(data, dtype="<f4" if layout.width == 4 else "<f8").astype(np.float64)
    elif layout.width == 2:
        samples = np.frombuffer(data, dtype="<i2") / 32768.0
    else:
        samples = np.array(layout.decode(data), dtype=np.float64) / float(1 << (8 * layout.width - 1))
    return samples.reshape(-1, layout.channels).mean(axis=1), rate


def number_beats(beats, period):
    """Beat numbers from 0, counted against the period so missed beats leave gaps."""
    return np.round((beats - beats[0]) / period)


def fit_grid(beats, downbeats, duration):
    """One BPM and one downbeat for detected beat and downbeat times (seconds)."""
    beats = np.sort(np.asarray(beats, dtype=np.float64))
    if len(beats) < 8:
        raise ValueError("Too few beats were detected to fit a grid.")
    intervals = np.diff(beats)
    median = np.median(intervals)
    # The mean of the ordinary intervals is exact to about 20 ms over the whole track,
    # which keeps the numbering right even hundreds of beats in.
    period = intervals[np.abs(intervals - median) < 0.1 * median].mean()
    index = number_beats(beats, period)
    keep = np.ones(len(beats), dtype=bool)
    for _ in range(4):
        period, start = np.polyfit(index[keep], beats[keep], 1)
        index = np.round((beats - start) / period)
        keep = np.abs(beats - (start + period * index)) < OUTLIER
        if keep.sum() < 8 or keep.mean() < 0.5:
            raise ValueError("The detected beats don't follow a steady tempo.")
    period, start = np.polyfit(index[keep], beats[keep], 1)
    best = np.sqrt(np.mean((beats[keep] - start - period * index[keep]) ** 2))

    # Electronic music is usually made at a whole BPM (else a half one); prefer it when it
    # fits as well, which also matches the BPM tag written on save.
    bpm, rounded = 60 / period, False
    for candidate in (round(bpm), round(bpm * 2) / 2):
        trial = 60 / candidate
        offset = np.mean(beats[keep] - trial * index[keep])
        error = np.sqrt(np.mean((beats[keep] - offset - trial * index[keep]) ** 2))
        if error <= best + ROUND_SLACK:
            bpm, period, start, rounded = float(candidate), trial, offset, True
            break

    residual = beats - (start + period * index)
    on_grid = float(np.mean(np.abs(residual) < ON_GRID))
    smoothed = np.convolve(residual[keep], np.ones(DRIFT_WINDOW) / DRIFT_WINDOW, mode="valid") if keep.sum() >= DRIFT_WINDOW else residual[keep]
    drift = float(np.max(np.abs(smoothed))) if len(smoothed) else 0.0

    # Which beat of the bar is 1: the place most detected downbeats fall on.
    votes = np.zeros(4)
    for downbeat in np.asarray(downbeats, dtype=np.float64):
        position = (downbeat - start) / period
        if abs(position - round(position)) * period < OUTLIER:
            votes[int(round(position)) % 4] += 1
    phase = int(np.argmax(votes)) if votes.sum() else 0
    downbeat_share = float(votes[phase] / votes.sum()) if votes.sum() else 0.0
    # Anchor on the downbeat nearest the middle, where a rounded BPM matters least.
    middle = round((duration / 2 - start) / period)
    middle += (phase - middle) % 4
    anchor = start + period * middle
    return {"bpm": bpm, "anchor": float(anchor), "rounded": rounded, "confidence": on_grid,
            "downbeatConfidence": downbeat_share, "drift": drift, "beats": int(len(beats))}


def onset_strength(samples, rate):
    """Per-millisecond rise in log energy: high where something starts (a kick's attack)."""
    hop = max(1, rate // 1000)
    window = 5 * hop
    energy = np.concatenate([[0.0], np.cumsum(samples ** 2)])
    starts = np.arange(0, max(0, len(samples) - window), hop)
    level = np.log(energy[starts + window] - energy[starts] + 1e-9)
    rise = np.zeros_like(level)
    rise[5:] = np.maximum(0, level[5:] - level[:-5])  # The 5 ms after each point against the 5 ms before.
    return rise, hop / rate


def refine_phase(samples, rate, grid):
    """Move the grid by up to SEARCH so its beats sit on the attacks, downbeats counting double."""
    rise, step = onset_strength(samples, rate)
    if not len(rise):
        return grid
    period = 60 / grid["bpm"]
    first = math.ceil(-grid["anchor"] / period)
    times = grid["anchor"] + period * np.arange(first, first + int(len(rise) * step / period) + 1)
    weights = np.where(np.round((times - grid["anchor"]) / period) % 4 == 0, 2.0, 1.0)
    shifts = np.arange(-SEARCH, SEARCH + step / 2, step / 2)
    scores = []
    for shift in shifts:
        frames = np.round((times + shift) / step).astype(int)
        valid = (frames >= 0) & (frames < len(rise))
        scores.append(float(np.sum(rise[frames[valid]] * weights[valid])))
    refined = dict(grid)
    refined["shift"] = float(shifts[int(np.argmax(scores))])
    refined["anchor"] = grid["anchor"] + refined["shift"]
    return refined


def detect(path, model):
    """The grid for a decoded WAV, using the beat_this checkpoint at `model`."""
    try:
        from beat_this.inference import Audio2Beats
    except ImportError:
        raise RuntimeError("Grid detection isn't installed. Run: zsh build.sh --detect")
    samples, rate = read_wav(path)
    beats, downbeats = Audio2Beats(checkpoint_path=model, device="cpu", dbn=False)(samples, rate)
    return refine_phase(samples, rate, fit_grid(beats, downbeats, len(samples) / rate))
