# Audio Sorter

Local macOS MP3 sorting app. No uploads; audio playback uses AVFoundation. Comments
are embedded ID3 tags written with Mutagen, without re-encoding the audio.

## Build / launch

Requires macOS, Xcode command-line tools, and Python 3. Internet is needed for the
first build to install Mutagen into this folder's `.venv`.

```sh
zsh build.sh
open "Audio Sorter.app"
```

Keep this folder and `.venv` in place: the app references its Python environment.

## Configuration

`config.json` contains your personal settings and is git-ignored, along with build
outputs and dependencies. Commit `config.example.json` as the shareable template.
The build copies that template to `config.json` only if no local config exists.
Edit the local config and restart the app; no rebuild is needed.

- `sourceFolder`: initial folder; defaults to `~/Downloads`. After choosing a folder,
  the app remembers it on this Mac.
- `destinationRoot`: parent directory for the destination folders; `~` is supported.
- `destinations`: 1–9 unique subfolder names, in shortcut order. Missing directories
  are created on the first move.
- `startAtThirtyPercent`: `true` by default, including when omitted. Each newly
  loaded track is positioned 30% into its duration; `false` starts at the beginning.

## Keys

| Key | Action |
| --- | --- |
| ⌘O | Choose any source folder (initially ~/Downloads) |
| Enter / Space | Play/pause |
| H / L or ← / → | Scrub backward/forward 5 seconds |
| Shift + H / L or Shift + ← / → | Scrub backward/forward 30 seconds |
| J / K or ↓ / ↑ | Select next/previous, without autoplay |
| N | Save comment and preview next, without moving |
| E | Edit embedded MP3 comment |
| ⌘Enter / ⌘S | Save comment and return to sorting |
| ⌘1–⌘9 (configured destinations) | Save comment, move, and preview next, even inside the comment editor |
| Esc, while editing | Discard unsaved comment edits |
| 1–9 (configured destinations) | Move to the corresponding folder and preview next |
| ⌘Z, outside comment editor | Undo last move |

Plain numbers and playback keys type normally in the comment editor. Use ⌘number
to move directly from the editor, or leave it with ⌘Enter and use plain numbers.
Clicking a destination also saves edits.
Changing track, folder, or quitting saves pending comments. Moving automatically
plays the next track. At the end, playback stops; it does not wrap around.

Only MP3s are listed. Subfolders are opt-in. Symbolic links are excluded. Existing
destination files are never overwritten; a collision displays an error and leaves
the original in place. Move undo is session-only and does not undo comment edits.
Comments preserve other tag frames and existing ID3v2.3/v2.4 format, with atomic
same-directory replacement. As with any tag editor, keep backups of valuable files.

Sort before importing into Rekordbox to avoid missing-file references. Files already
in Rekordbox may need relocating after moves, and tag reloads after comment edits.

## Tests

Run `.venv/bin/python -m unittest -v`. Tests use temporary files only. FFmpeg is
needed for the audio-preservation integration test; that test skips if unavailable.
