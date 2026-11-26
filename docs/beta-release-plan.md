# Plan: a downloadable beta

Status: §1 built (`zsh build.sh --release`, checked by `test_release.py`); §2–4 next.
CI is in place (`.github/workflows/ci.yml`: build with detection and
the full suite on an Apple Silicon runner). Decisions agreed: Apple Silicon only,
detection included in the download, and signing/notarization wired up now but only
switched on once an Apple Developer ID exists; until then betas are unsigned.

## Goal

A tagged commit (`v0.9.0-beta.1`) produces a DMG on a GitHub prerelease that a tester
can drag to Applications and run, with no repo checkout, `.venv`, Homebrew Python or
`build.sh`. Local development keeps working exactly as now.

## Licence and publisher

Decided: GPL-3.0-or-later, copyright Bottoni Music LLC (`LICENSE`, README, About
window). This also covers shipping Mutagen (GPL-2.0-or-later). Other bundled parts:
PyTorch (BSD), beat_this code and weights (MIT), CPython (PSF). FFmpeg stays a separate
`brew install ffmpeg`, which keeps LAME/GPL builds out of the bundle; MP3 edits already
explain this when FFmpeg is missing.

- **Apple Developer Program:** enrol as the organization Bottoni Music LLC, so Gatekeeper
  shows the LLC as the developer. Needs a D-U-N-S number (free, days to a couple of
  weeks) and the same $99/year. The name on a Developer ID certificate can't be changed
  later, so don't enrol personally first.
- **Bundle ID:** reverse DNS of a domain the LLC controls (for example
  `com.bottonimusic.audiosorter`), or `io.github.samb0t.audiosorter` without one. Pick
  it once before the first beta; changing it later resets testers' settings.

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
- **Bundle ID:** `com.bottonimusic.audiosorter` (decided). Changing it moves
  `UserDefaults` (remembered source folder, Snap and Click toggles); the three keys are
  copied across from `local.audio-sorter` once on first launch.
- **Size:** measured: the app is 932 MB (PyTorch 626 MB); the DMG is 320 MB with LZMA
  (`hdiutil -format ULMO`, about 3 minutes) against 419 MB with the default zlib (UDZO).
  Use ULMO. Trimming (unused torch libraries, `torchaudio` if beat_this only needs
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
- Without the secrets the same workflow ad-hoc signs (Apple Silicon refuses to run
  unsigned code at all, so every binary, including the embedded Python's libraries,
  gets an ad-hoc signature). See "Before the Developer ID" for what testers do.

### Before the Developer ID

Since macOS 15, right-click → Open no longer gets past Gatekeeper for an app that isn't
notarized. A tester who downloads the ad-hoc-signed DMG has two ways in:

1. Open the app once and dismiss the "Apple could not verify…" alert, then
   **System Settings → Privacy & Security → Open Anyway**, and confirm with their
   password. Needed once per installed version.
2. Or in Terminal, once per download:
   `xattr -dr com.apple.quarantine "/Applications/Audio Sorter.app"`.

The release notes carry both, with screenshots for option 1. Technical testers can skip
this entirely by building from source (`zsh build.sh --detect`): a locally built app
isn't quarantined. Keep the unsigned phase to a small, invited group, since asking
strangers to override Gatekeeper is exactly what malware asks for too. Homebrew casks
aren't an option until the app is notarized.

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

- Bundle ID (above) and a short `PRIVACY`/README note: no network use apart from the
  build-time model download.
- A "Beta" section in the README: install, FFmpeg for MP3 edits, how to report bugs.
- Test the DMG on a Mac that has never had the repo: first launch, choosing folders,
  sorting, a WAV and an MP3 edit, Detect, Rekordbox XML.

## Later

- Auto-update (Sparkle), Intel/universal builds, bundling FFmpeg (licensing review
  first), persisting unsaved edits across launches.
