# Plan: automatic grid detection with beat_this

Status: planned, not built. Decisions agreed: detection runs only when asked (a Detect
button), its result is applied directly as the grid (downbeat set), the model is
`final0`, and installing PyTorch into the app's `.venv` is fine.

## Goal

In Cut Bars, **Detect** fills in the BPM and downbeat for the current track, so the
usual job becomes checking the grid rather than setting it. Everything after that is
unchanged: nudges, ⌘\ to move the downbeat, ÷2/×2, cues, edits, recording for
Rekordbox. Only constant-tempo 4/4 grids, as now.

## Background

[beat_this](https://github.com/CPJKU/beat_this) (Foscarin, Schlüter, Widmer, ISMIR 2024)
predicts beat and downbeat times from audio. Code and weights are MIT licensed. It
needs PyTorch 2, `einops`, `soxr` and `rotary-embedding-torch`; `final0` is about 78 MB
and is downloaded on first use unless provided. Python API:

```python
from beat_this.inference import File2Beats
beats, downbeats = File2Beats(checkpoint_path="final0", device="cpu", dbn=False)(path)
```

Its output is frame-based at 50 frames per second, so single beats are only accurate to
about 20 ms, and beats follow tempo drift; the editor needs one BPM and one downbeat.

## 1. Installation (opt-in)

- `requirements-detect.txt`: `torch`, `beat-this`, plus its dependencies, pinned.
- `zsh build.sh --detect` installs it into `.venv` and pre-downloads `final0` into a
  folder inside the repo's `.build/` (git-ignored), so detection never needs the
  network or the user's torch cache. Plain `zsh build.sh` is unchanged.
- The app finds out whether detection is available with a cheap helper action
  (`detect-available`), and the Detect button explains how to enable it otherwise.
- Device: CPU by default; try Apple's `mps` and keep it only if it is faster and gives
  the same beats.

## 2. Same timeline as the editor

beat_this would decode MP3s with FFmpeg, which differs from AVFoundation by about 12 ms
on MP3s without a gapless header. As for MP3 edits, the app decodes the file with
AVFoundation (`decodeToWAV`, already used when saving) into a temporary WAV and passes
that, so detected times are on the editor's timeline for every format. The temporary
file lives in the system temp folder and is always removed.

## 3. From beats to a grid (`detect.py`)

A new helper module, called as `comments.py detect <wav>`, returns
`{"bpm", "anchor", "confidence", "drift", "beats", "downbeats"}`:

1. **Run the model** on the decoded WAV with `final0`, without the DBN.
2. **Tempo**: take the median beat interval, number each beat by rounding against it
   (so missed or extra beats don't shift later numbers), then fit beat time against
   beat number with a least-squares line, dropping beats more than 40 ms off and
   refitting. The slope gives the BPM.
3. **Prefer round tempos**: if the nearest whole BPM (then half BPM) fits the beats
   almost as well (RMS within 1 ms of the free fit), use it. Electronic music is
   usually produced at a whole BPM, and a rounded value matches the BPM tag written
   on save.
4. **Which beat is 1**: assign each detected downbeat to its beat number and take the
   most common position mod 4. The margin over the runner-up is the downbeat
   confidence.
5. **Fine phase**: the 20 ms frames limit the line's offset. Refine it by sliding the
   whole grid within ±15 ms to maximise the sum of an onset envelope (rectified
   high-passed energy rise, computed with numpy on the same WAV) at beat positions,
   weighted towards downbeats. This lands the lines on kick attacks.
6. **Anchor**: return the downbeat nearest the middle of the track, so rounding the
   BPM matters least where it is most audible.
7. **Report**: confidence = share of detected beats within 20 ms of the grid; drift =
   largest gap between grid and beats over the track. Above about 30 ms, warn that the
   tempo is not constant and the grid will need care.

## 4. In the editor

- A **Detect** button in the grid row (no key yet). It runs in the background with a
  spinner and the status line "Detecting beats…"; a second press or opening another
  track cancels it (the helper process is terminated).
- On success it applies the grid as if typed and placed: `tempoSet` and `downbeatSet`
  become true, and the selection, cues and edits are remapped exactly as for ⌘\ (one
  undoable step, so ⌘Z returns to the previous grid). The status line reports, for
  example, "Detected 124 BPM, bar 1 at 0:00.512, 97% of beats on the grid. Check a
  late bar with the click (K)." plus the drift warning when it applies.
- Low confidence (under about 80%, or an unclear downbeat) still applies the grid but
  says so, and suggests ÷2/×2 or ⌘\ on the first kick.
- Detection never writes tags or records anything: saving, sorting and ⌘S/⌘⇧S work as
  now.
- Grid changes are not on the edit undo stack today; detection adds a grid entry to
  it, so that part of the stack needs a small extension (pieces, cues and grid
  snapshots together).

## 5. Tests

- **Grid fitting** (`test_detect.py`, no PyTorch needed): synthetic beat lists with
  jitter, 20 ms quantisation, missed and extra beats, a pickup before bar 1, a wrong
  downbeat on a few bars, half/double tempo, and slow drift. Check the BPM, the anchor
  to within 1 ms, round-tempo preference, the downbeat vote and the drift warning.
- **Phase refinement**: synthetic click audio with a known offset.
- **End to end** (skipped unless beat_this is installed): click tracks from
  `calibrate.py` and a short synthetic house loop, run through the helper.
- **Native**: the Detect button applies the grid, undo restores the previous one,
  cancelling leaves the session unchanged, and the "not installed" message.
- **Real tracks**, by hand: run on a few tracks whose grids were set manually (read
  only), compare BPM and downbeat, and record the results in the README.

## 6. Speed

Measure `final0` on CPU (and `mps`) for a 6-minute track; the model loads once per
helper run, so keep a single process per detection for now. If it is fast enough, a
later step could detect in the background for upcoming tracks while sorting, so the
grid is ready when Cut Bars opens.

## 7. Documentation

README: Build section (`--detect`), Cut Bars step 1 (Detect, then check), the key table
if a key is added, Tests (the skipped end-to-end test), and the TODO entry.
