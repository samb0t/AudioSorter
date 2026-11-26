# Plan: a downloadable beta

Status: planned. CI is in place (`.github/workflows/ci.yml`: build with detection and
the full suite on an Apple Silicon runner). Decisions agreed: Apple Silicon only,
detection included in the download, and signing/notarization wired up now but only
switched on once an Apple Developer ID exists; until then betas are unsigned.

## Goal

A tagged commit (`v0.9.0-beta.1`) produces a DMG on a GitHub prerelease that a tester
can drag to Applications and run, with no repo checkout, `.venv`, Homebrew Python or
`build.sh`. Local development keeps working exactly as now.

## Decision needed first: licence

The repo has no LICENSE, and Mutagen, which the app ships and imports in `comments.py`,
`edit.py` and `rekordbox.py`, is GPL-2.0-or-later. Running it from your own checkout is
fine; distributing it inside the app generally means distributing the app under
GPL-compatible terms (for example GPL-3.0-or-later, with the source on GitHub). The
alternatives are keeping the repo private and the beta closed, or replacing Mutagen,
which would mean re-implementing the ID3/RIFF writer that the safety guarantees rest
on. Settle this before the first public build. Other bundled parts: PyTorch (BSD),
beat_this code and weights (MIT), CPython (PSF). FFmpeg stays a separate
`brew install ffmpeg`, which keeps LAME/GPL builds out of the bundle; MP3 edits already
explain this when FFmpeg is missing.

## Why the current app can't be shared

`build.sh` writes absolute paths into the bundle: `python-path` (the repo's `.venv`),
`config-path` (the repo's `config.json`) and `detect-model` (`.build/beat_this/`), read
at `main.swift:125`, `main.swift:399` and `main.swift:737`. It is ad-hoc signed, so a
downloaded copy is quarantined by Gatekeeper. The bundle ID is `local.audio-sorter`.

## 1. Self-contained bundle

- **Python:** a relocatable CPython 3.12 for `aarch64-apple-darwin` from
  [python-build-standalone](https://github.com/astral-sh/python-build-standalone),
  pinned by version and SHA-256 like the model, unpacked to
  `Contents/Resources/python/`, with `requirements.txt` and `requirements-detect.txt`
  installed into it. No `.venv`, no `pip` at run time; drop `pip`, tests and
  `__pycache__` to save space.
- **Model:** `final0.ckpt` copied to `Contents/Resources/beat_this/final0.ckpt`.
- **Lookup order in `main.swift`:** keep the three path files for development builds;
  when a file is absent, use the bundled location. `build.sh` is unchanged for local
  use; a new `build.sh --release` (or `package.sh`) assembles the bundled variant.
- **Config:** release builds read and write
  `~/Library/Application Support/Audio Sorter/config.json` (where `grids.json` already
  lives), created from the bundled `config.example.json` on first launch. Development
  builds keep the repo's `config.json`.
- **Version:** `CFBundleShortVersionString` and `CFBundleVersion` from the tag
  (`0.9.0-beta.1` → `0.9.0`, build number from the run), shown in About.
- **Bundle ID:** a real reverse-DNS ID, for example `io.github.samb0t.audiosorter`.
  Changing it moves `UserDefaults` (remembered source folder, Snap and Click toggles);
  copy the three keys across from `local.audio-sorter` once on first launch.
- **Size:** PyTorch alone is 626 MB unpacked here. Measure the DMG; expect roughly
  200–300 MB. Trimming (unused torch libraries, `torchaudio` if beat_this only needs
  `soxr`) is a follow-up, verified by the detection tests.

## 2. Signing and notarization (when the Developer ID exists)

- Sign inside out with `--options runtime --timestamp`: every `.so`/`.dylib` and the
  Python executable, then the app. Entitlements only if PyTorch needs them under the
  hardened runtime (`com.apple.security.cs.disable-library-validation` or
  `allow-unsigned-executable-memory`); start with none and add only what a failing
  detection test shows is required.
- `xcrun notarytool submit --wait` with an App Store Connect API key, then
  `xcrun stapler staple` on the app and the DMG.
- GitHub secrets: `DEVELOPER_ID_P12` (base64), `DEVELOPER_ID_P12_PASSWORD`,
  `NOTARY_KEY_P8` (base64), `NOTARY_KEY_ID`, `NOTARY_ISSUER_ID`. The workflow imports
  the certificate into a temporary keychain and deletes it afterwards.
- Without the secrets the same workflow ad-hoc signs, and the release notes tell
  testers to right-click → Open (or `xattr -dr com.apple.quarantine`).

## 3. Release workflow

`.github/workflows/release.yml`, on tags matching `v*`, on `macos-26`:

1. Run the same build and tests as CI.
2. Assemble the self-contained app, sign (Developer ID or ad hoc), notarize if
   possible.
3. **Smoke test the bundle**, not the repo: launch its Python to import `mutagen` and
   `beat_this`, run detection on a generated click track, and check that no path in
   `Contents/Resources` points outside the bundle.
4. `hdiutil create` a DMG with the app and an Applications link; sign and notarize it.
5. Create a GitHub prerelease with the DMG, its SHA-256, and notes taken from the tag
   message.

## 4. Before the first tag

- Licence (above) and a short `PRIVACY`/README note: no network use apart from the
  build-time model download.
- A "Beta" section in the README: install, FFmpeg for MP3 edits, how to report bugs.
- Test the DMG on a Mac that has never had the repo: first launch, choosing folders,
  sorting, a WAV and an MP3 edit, Detect, Rekordbox XML.

## Later

- Auto-update (Sparkle), Intel/universal builds, bundling FFmpeg (licensing review
  first), persisting unsaved edits across launches.
