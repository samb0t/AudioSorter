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
Open **Settings…** in the toolbar or app menu (**⌘,**) to choose the source and
destination parent directories, add/remove up to nine destination folders, and
edit each folder's Genre mapping. Rows are numbered in shortcut order; a blank
Genre preserves the existing tag. You can also toggle the 30% preview start.
**Save** writes `config.json` atomically and applies changes immediately; **Cancel**
discards edits. Changing the configured source loads it without autoplay and
replaces the previously remembered folder. Destination/Genre changes affect future
moves only; existing files are not moved or retagged, and move undo remains available.
You can still edit the local config manually and restart; no rebuild is needed.

- `sourceFolder`: initial folder; defaults to `~/Downloads`. After choosing a folder,
  the app remembers it on this Mac.
- `destinationRoot`: parent directory for the destination folders; `~` is supported.
- `destinations`: 1–9 unique subfolders, in shortcut order. Entries can be legacy
  strings (move only) or objects with `directory` and optional `genre`:
  ```json
  "destinations": [
    { "directory": "House", "genre": "House" },
    { "directory": "Tech-House", "genre": "Tech House" },
    { "directory": "Review" }
  ]
  ```
  Missing directories are created on the first move. A configured `genre` replaces
  the standard ID3 Genre (TCON), not Rekordbox My Tags. Omit it to preserve Genre;
  empty values are rejected. Hover over a destination button to see its tagging
  behavior. Choosing the file's current directory remains a no-op, not an in-place
  retag operation.
- `startAtThirtyPercent`: `true` by default, including when omitted. Each newly
  loaded track is positioned 30% into its duration; `false` starts at the beginning.

## Keys

| Key | Action |
| --- | --- |
| ⌘, | Open Settings |
| ⌘W | Close the focused window (closing Settings discards unsaved settings) |
| ⌘O | Choose any source folder (initially ~/Downloads) |
| ⌘R | Refresh the file list after changes in Finder (also works while editing comments) |
| ⌘⇧R | Show the current MP3 in Finder |
| ⌘Delete | Move the current MP3 to Trash, after confirmation (also while editing comments) |
| Enter / Space | Play/pause |
| H / L or ← / → | Scrub backward/forward 5 seconds |
| Shift + H / L or Shift + ← / → | Scrub backward/forward 30 seconds |
| ⌘H / ⌘L | Scrub backward/forward 5 seconds, including while editing comments |
| ⌘⇧H / ⌘⇧L | Scrub backward/forward 30 seconds, including while editing comments |
| ⌘J / ⌘K | Save comment and select next/previous track, without autoplay; keep current focus |
| J / K or ↓ / ↑ | Select next/previous, without autoplay |
| N | Save comment and preview next, without moving |
| E | Edit embedded MP3 comment |
| ⌘Enter / ⌘S | Save comment and return to sorting |
| ⌘1–⌘9 (configured destinations) | Save comment, move, and preview next, even inside the comment editor |
| Esc, while editing | Discard unsaved comment edits |
| 1–9 (configured destinations) | Move to the corresponding folder and preview next |
| ⌘Z, outside comment editor | Undo last move and its Genre change |

Plain numbers and playback keys type normally in the comment editor. Use ⌘number
to move directly from the editor, or leave it with ⌘Enter and use plain numbers.
In the sorting window, ⌘H scrubs backward instead of hiding the app, regardless of
focus. The ⌘H/L and ⌘J/K shortcuts work both inside and outside the comment editor.
Scrubbing keeps the current focus and leaves unsaved text unchanged.
Clicking a destination also saves edits.
Use **Show in Finder** in the toolbar or File menu to reveal the current MP3, or
right-click a list item to reveal that file without changing tracks. This does not
move files, change playback, or save pending comment edits.
**Move to Trash…** is available in the File menu and by right-clicking a list item.
It always asks for confirmation, with Cancel as the default. Right-click targets
that item, not necessarily the current track. Pending comments on a track being
trashed are saved first; a save or Trash failure leaves it in the queue. Deleting
another track preserves the current track and its unsaved comments. Deleting the
current track selects the next (or previous at the end) and continues playback
only if it was already playing. Recover trashed files in Finder; the app’s Undo
Move command does not undo deletion. Plain Delete still edits text normally.
Changing track, folder, or quitting saves pending comments. Moving automatically
plays the next track. At the end, playback stops; it does not wrap around.

Only MP3s are listed. Subfolders are opt-in. Symbolic links are excluded.
Rescan with **Refresh** beside the source path or in the File menu.
Refresh saves pending comments, then picks up additions, removals, and renames.
If the current file's path remains listed, selection and playback position are
preserved; otherwise the first remaining track is selected without autoplay.
If saving a comment fails (for example, its file was moved in Finder), refresh is
cancelled so unsaved text stays available. Refresh is manual, not a folder watcher.

Existing destination files are never overwritten; a collision displays an error and leaves
the original in place. A failed Genre write rolls back the move; if rollback also
fails, the error reports the remaining file location for recovery. Move undo is
session-only, restores the previous Genre (including an absent Genre), and does not
undo comment edits. Undo refuses to overwrite files at the original path.
Comments preserve other tag frames and existing ID3v2.3/v2.4 format, with atomic
same-directory replacement. As with any tag editor, keep backups of valuable files.

Sort before importing into Rekordbox to avoid missing-file references. Files already
in Rekordbox may need relocating after moves, and tag reloads after comment or Genre
edits. Create Rekordbox Intelligent Playlists using conditions such as `Genre equals
House` to automatically group imported tracks. These playlists do not watch folders
or import files automatically. Keep gig playlists as curated regular playlists.

## Check destination tags

Choose **File → Check destination tags…** to lint the configured destination
folders against their Genre mappings. The check runs in the background, includes
subfolders, and never changes files, tags, pending comments, selection, or playback.
Destinations without a Genre mapping are skipped, as are hidden files/directories
and symbolic links. Missing destination folders and directory/tag read failures
are reported alongside missing or mismatched Genre tags (including extra values).

The results window shows the expected and actual Genre and the issue for each
file. Select an issue and click **Show in Finder**; if its path is missing, Finder
reveals the nearest existing parent. **Check again** uses the latest settings.
Results are a snapshot: check again after sorting, editing tags, or changing
settings. This checks consistency with your rules, not the musical genre itself.
There are no automatic fixes.

## Tests

Run `.venv/bin/python -m unittest -v`. Tests use temporary files only. FFmpeg is
needed for the audio-preservation integration test; that test skips if unavailable.
