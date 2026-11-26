## Install

Requires an Apple Silicon Mac with macOS 14 or later.

1. Download the DMG below, open it, and drag **Audio Sorter** to **Applications**.
2. This beta isn't notarized by Apple yet, so macOS blocks the first launch. Open the
   app once and dismiss the "Apple could not verify…" message, then go to
   **System Settings → Privacy & Security**, scroll down, click **Open Anyway** and
   confirm. (On macOS 14 you can instead right-click the app and choose **Open**.)
   Or, in Terminal:
   `xattr -dr com.apple.quarantine "/Applications/Audio Sorter.app"`
3. MP3 edits in Cut Bars need FFmpeg: `brew install ffmpeg`. Everything else works
   without it.

Only do step 2 for a DMG downloaded from this page, and check it first if you like:
`shasum -a 256` should match the `.sha256` file below.
