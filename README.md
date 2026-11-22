# Audio Sorter

Local macOS MP3, WAV, and AIFF (`.aif` / `.aiff`) sorting app. No uploads;
audio playback uses AVFoundation. Format-aware embedded comments and Genre edits
never re-encode audio. Mutagen decodes/encodes individual ID3 frames; untouched
frames and container chunks are preserved byte-for-byte.

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
moves and explicitly confirmed Genre repairs only; existing files are not
automatically moved or retagged, and move undo remains available.
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
   Genre (ID3 `TCON` for MP3/AIFF; RIFF INFO `IGNR` for WAV), not Rekordbox
   My Tags. Omit it to preserve Genre;
  empty values are rejected. Hover over a destination button to see its tagging
  behavior. Choosing the file's current directory remains a no-op, not an in-place
  retag operation.
- `startAtThirtyPercent`: `true` by default, including when omitted. Each newly
  loaded track is positioned 30% into its duration; `false` starts at the beginning.
- `editSuffix`: name used for bar edits, saved as `<name> - <editSuffix>.<ext>`
  beside the original; `Edit` when omitted. Also editable in Settings.

## Keys

| Key | Action |
| --- | --- |
| ⌘, | Open Settings |
| ⌘W | Close the focused window (closing Settings discards unsaved settings) |
| ⌘O | Choose any source folder (initially ~/Downloads) |
| ⌘R | Refresh the file list after changes in Finder (also works while editing comments) |
| ⌘⇧R | Show the current audio file in Finder |
| ⌘Delete | Move the current audio file to Trash, after confirmation (also while editing comments) |
| Enter / Space | Play/pause |
| H / L or ← / → | Scrub backward/forward 5 seconds |
| Shift + H / L or Shift + ← / → | Scrub backward/forward 30 seconds |
| ⌘H / ⌘L | Scrub backward/forward 5 seconds, including while editing comments |
| ⌘⇧H / ⌘⇧L | Scrub backward/forward 30 seconds, including while editing comments |
| ⌘J / ⌘K | Save comment and select next/previous track, without autoplay; keep current focus |
| J / K or ↓ / ↑ | Select next/previous, without autoplay |
| N | Save comment and preview next, without moving |
| E | Edit embedded comment |
| B / ⌘B | Open the current track in **Cut Bars** (pauses playback; saves the comment first) |
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
The waveform shows loudness (RMS) across the track, so breakdowns appear as dips;
click or drag on it to seek. It is computed locally in the background, read-only,
and kept in memory for the session.
Clicking a destination also saves edits.
Use **Show in Finder** in the toolbar or File menu to reveal the current audio file, or
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

MP3, WAV, `.aif`, and `.aiff` files are listed (case-insensitive). Subfolders are
opt-in. Hidden files and symbolic links are excluded. FLAC/M4A are not listed.
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
Edits preserve other tag frames and existing ID3v2.3/v2.4 format, with verified,
atomic same-directory temporary-file replacement. Artwork, DJ-specific frames,
opaque chunks, audio payloads, and chunk alignment bytes are copied unchanged.
As with any tag editor, keep backups of valuable files.

### Metadata formats and safety limits

| Format | Comment | Genre |
| --- | --- | --- |
| MP3 | ID3 `COMM` | ID3 `TCON` |
| AIFF (`.aif`, `.aiff`) | ID3 `COMM` in `ID3 ` chunk | ID3 `TCON` |
| WAV | RIFF `LIST/INFO/ICMT` | RIFF `LIST/INFO/IGNR` |

Pioneer DJ's [rekordbox metadata reference](https://forums.pioneerdj.com/hc/en-us/articles/205021569-rekordbox-metadata)
explicitly identifies WAV comments as `ICMT` and Genre as `IGNR`. WAV ID3-only
Genre was ignored by Reload Tag in the previously reported test; updating `IGNR`
fixed that tested WAV. Do not rely on an ID3 inspector's Genre for WAV compatibility.
WAV comments read/write `ICMT` only; existing ID3 comments are preserved, not used
as a fallback or synchronized (including any old ID3-only comment).

WAV Genre writes also synchronize an **existing** ID3 `TCON` to prevent stale,
conflicting values; they do not create ID3 tags just for Genre. Genre undo restores
both prior fields independently, including conflicts, original raw frame/text
bytes, and absent fields. Comments edited after a move survive Genre undo.

RIFF INFO text has no universal Unicode encoding. For now WAV comment/Genre edits
accept ASCII only (no embedded NUL); non-ASCII or malformed existing target fields
are reported as unsupported, not guessed or silently converted. Unicode remains
supported for MP3/AIFF ID3 comments and Genre. International WAV encoding support
requires separate Rekordbox verification.

Malformed chunk sizes, duplicate INFO fields/lists or ID3 chunks/frames, ID3v2.2,
extended/flagged/unsynchronized ID3, and RF64/RIFX WAV are rejected rather than
repaired. The same checks apply to linting. Supported RIFF WAVE and FORM AIFF/AIFC
layouts must have exactly one audio-data chunk and one format chunk. Writes use
in-memory copies, so very large files may require substantial memory. A failed
write leaves the original intact and cleans up its temporary file.

**Verification status:** automated temporary-fixture checks cover all four filename
extensions, AVFoundation loading/preview position, discovery/refresh, mocked Trash,
move/tag rollback, collisions, Genre undo, and byte-identical audio packets/chunks.
No manual Rekordbox import or Reload Tag verification was performed for this change.
On disposable copies, verify import and Reload Tag for comments and Genre per format,
then verify Genre undo/reload and WAV RIFF/ID3 conflict handling. Do not test on the
music library without backups.

Sort before importing into Rekordbox to avoid missing-file references. Files already
in Rekordbox may need relocating after moves, and tag reloads after comment or Genre
edits. Create Rekordbox Intelligent Playlists using conditions such as `Genre equals
House` to automatically group imported tracks. These playlists do not watch folders
or import files automatically. Keep gig playlists as curated regular playlists.

## Cut Bars

**Cut Bars…** (toolbar, **B**, or **File → Cut Bars… ⌘B**) opens the current track in
an editor for removing or repeating whole bars, the way you would in Ableton:

1. **Set the grid.** The BPM comes from the file's BPM tag when present (Beatport
   and most stores set one); otherwise type it, or tap **T** (or **Tap**) along with
   the beat: after four taps the BPM becomes the running average, to 0.1 BPM, and
   a 2-second pause starts over. Tapping sets tempo only, not the downbeat. Zoom in on the first kick, click to
   put the playhead on it, and press **D** to make it a downbeat. Then check bar
   lines late in the track. If they drift off the kicks, fine-tune the BPM (decimals
   are fine). Turn on **Click** (**M**) to hear a metronome on every beat, accented
   on downbeats, and listen for flamming against the kicks. The click is generated
   live in sync with playback (measured within 0.1 ms) and follows the edited track
   across splices. On an unedited track, BPM changes, taps, and nudges are heard
   immediately while playing; once there are edits, grid changes pause playback. **,** / **.** nudge the grid 1 ms (Shift: 10 ms), and ⌥-drag slides it.
2. **Select bars.** **I** starts a selection at the bar line nearest the playhead.
   Choose a length (8/16/32 or type one), or press **O** at its end.
3. **Edit.** **X** (or Delete) cuts the selection; **⌘D** duplicates it right
   after itself and selects the copy, so pressing ⌘D again keeps extending. Edits
   stack. For example, cut a 32-bar breakdown, then select the last 16 bars and
   duplicate them. ⌘Z / ⌘⇧Z undo and redo.
4. **Listen.** The editor shows and plays the *edited* track; red lines mark splices.
   **P** plays from two bars before the selection, which after a cut or duplicate is
   right across the new splice.
5. **Save** (**⌘S**) writes `<name> - <editSuffix>.<ext>` next to the original. The
   original is never modified and an existing edit is never overwritten (move it to
   Trash first). The editor then closes and the new edit is selected in the main
   list, so 1–9 sorts it right away. The original stays listed, usually just above
   it alphabetically (other versions of the same title can sort in between), so you
   can sort or trash it too. Reopening the original (B) restores its grid and edits.

**Snapping.** Once the grid is placed (D, a nudge, or ⌥-drag), clicks snap to the
finest grid division at least 12 px wide: sixteenths and eighths when zoomed in, then
beats, half bars, bars, and multiples of bars as you zoom out. The grid draws exactly
those lines. ⌘-click places the playhead freely; the **Snap to grid** checkbox turns
snapping off (remembered). Before a downbeat is set, clicks are always free so you
can put the playhead on the first kick.

| Key (editor) | Action |
| --- | --- |
| Space | Play/pause |
| Click | Place the playhead (snaps once the grid is placed; ⌘-click is free) |
| ← / → | Previous/next beat; Shift: bar |
| Scroll / swipe | Move along the track |
| Pinch, ⌘/⌥-scroll, = / − | Zoom (down to individual samples); 0 fits the track |
| Overview strip | Click or drag to jump |
| D | Downbeat at playhead |
| T | Tap tempo |
| M | Metronome click on/off (remembered) |
| , / . | Nudge grid 1 ms earlier/later; Shift: 10 ms |
| ⌥-drag | Slide the grid |
| I / O | Selection start/end at the nearest bar line |
| X or Delete | Cut selection |
| ⌘D | Duplicate selection |
| ⌘Z / ⌘⇧Z | Undo/redo edits |
| P | Play into the selection |
| ⌘S | Save edit |
| Esc | Stop (or leave the BPM/bars field without applying) |

Grid, edits, and undo history are remembered per track until the app quits, so
closing the editor never loses anything. Unsaved edits show as "• unsaved" in the
editor and as the dot in its close button. Quitting (or closing the main window)
with unsaved edits lists the affected tracks and asks first, with Cancel as the
default. Grid-only changes (BPM, downbeat) don't count as unsaved. Edits
are stored as bar numbers, so tweaking the BPM or downbeat afterwards keeps them on
the same bars. Only 4/4 at a constant tempo is supported.

**How the file is written.** Every splice gets a 5 ms equal-gain crossfade, placed
just *before* the bar line: the audio before the splice fades into the audio leading
up to the next downbeat, so the transient on the downbeat itself is untouched and
there is no click. Playback in the editor uses hard splices; the saved file has the
fades.
- **WAV/AIFF:** lossless. Sample data outside the fades is copied byte-for-byte, and
  all other chunks (tags, artwork, `bext`, unknown chunks) are preserved. Supports
  8/16/24/32-bit integer and 32/64-bit float PCM, including WAVE_FORMAT_EXTENSIBLE
  and AIFC `sowt`/`fl32`; compressed formats are rejected.
- **MP3:** re-encoded once with FFmpeg's LAME encoder (`brew install ffmpeg`), at the
  nearest standard bitrate for CBR sources or V0 for VBR. The audio is decoded with
  AVFoundation, which the editor also uses to draw and play, so splices land exactly
  where they appeared. (FFmpeg's own decoder can disagree by ~500 samples on MP3s
  without a gapless header.) The ID3 tag, including artwork and DJ frames, is copied
  over unchanged. The output has a LAME gapless header, so its length is exact.
- **Safety checks.** The original is only ever read. The edit is written to a hidden
  temporary file in the same folder, flushed to disk, parsed again, and only then
  linked into place under its final name. A crash or full disk therefore never leaves
  a half-written `- SamEdit` file, and the link step fails rather than replacing an
  existing file. Container layouts the parser is not certain about are refused rather
  than guessed. Finally the app reads the finished file back with macOS's decoder: if
  its length is not exactly the edited length, it is moved to Trash and the save
  is reported as failed.
- **Timeline metadata is dropped**, because it would point at the wrong places:
  WAV `cue `/`smpl`/`acid`/`plst`/`LIST adtl`, AIFF `MARK`/`INST`, ID3 `TLEN`,
  `MLLT`/`ETCO`/`SYTC`/`SYLT`/`ASPI`/`POSS`, Serato `GEOB` frames, and Traktor
  `PRIV:TRAKTOR4`. The save message lists anything removed. Re-analyze the edit in
  your DJ software.

## Check destination tags

Choose **File → Check destination tags…** to lint the configured destination
folders against their Genre mappings. The check runs in the background, includes
subfolders, and never changes files, tags, pending comments, selection, or playback.
Destinations without a Genre mapping are skipped, as are hidden files/directories
and symbolic links. Missing destination folders and directory/tag read failures
are reported alongside missing or mismatched Genre tags (including extra values).
All supported audio formats are checked. WAV checks use `IGNR`, not ID3 as a
fallback: missing RIFF Genre or conflicting RIFF/ID3 values are findings, even when
an ID3 inspector shows the expected Genre. Unsupported/ambiguous metadata is a read
error finding, never a successful match or an automatic repair.

The results window shows the expected and actual Genre and the issue for each
file. Select an issue and click **Show in Finder**; if its path is missing, Finder
reveals the nearest existing parent. **Check again** uses the latest settings.
Results are a snapshot: check again after sorting, editing tags, or changing
settings. This checks consistency with your rules, not the musical genre itself.
Scanning remains read-only, with an animated status indicator while checking.
**Repair selected Genres…** is enabled when the selection contains eligible
missing-Genre findings or WAV RIFF/ID3 conflicts (multiple selection is supported).
Only those eligible rows are included, even in a mixed selection. The confirmation
lists every affected file, current tags, and intended Genre, with **Cancel** as the
default. Repairs include nested folders and always use the mapped **Expected Genre**:
- Missing-Genre repairs fill absent or empty fields only.
- WAV RIFF/ID3 conflict repairs (including ID3-only WAV Genres) **overwrite both
  RIFF INFO `IGNR` and ID3 `TCON`** to match Expected Genre, even if neither existing
  value matches it. Existing ID3 versions and unrelated metadata are preserved.

Other Genre mismatches (including whitespace-only tags without a WAV conflict),
malformed/unreadable metadata, and missing directories remain report-only.
The selection hint explains eligibility. Repairs never create directories or move files.

The separate background write operation rechecks the current on-disk configuration,
file identity/content snapshot, path containment, symlinks, and current tags before
each write and again before atomic replacement. Stale findings or changed mappings
are skipped. MP3/AIFF use ID3 `TCON`; WAV uses RIFF INFO `IGNR` and synchronizes an
existing ID3 Genre. The same lossless, verified same-directory writer used by
sorting preserves audio payloads, artwork, DJ metadata, and unrelated frames/chunks;
audio is never re-encoded. Existing WAV ASCII-only and unsupported-layout limits apply.

While repairs run, sorting, comment saves, Trash, move undo, settings saves, and
track/folder changes that would save comments are temporarily blocked. Playback
controls remain available, and repairs do not save/discard pending text, change the
current track/selection, or restart playback. A second repair cannot run concurrently.
Per-file successes, skips, and failures remain in the results window. After repair,
only the attempted files are rechecked against current on-disk mappings: resolved
rows disappear, and remaining issues get fresh tags and file snapshots. Unrelated
rows, surviving selections, and the scroll position are preserved; destination
folders are not enumerated again. The summary makes clear that untouched results
remain snapshots. **Check again** still performs a full scan using current settings.
If a targeted recheck fails, previous rows are retained with a warning rather than
assumed resolved. A failed file does not stop the rest.

**Repair undo:** there is no app undo for these explicit repairs; keep backups.
Repairs neither add to nor clear/change the existing move/Genre undo stack. Undo Move
still restores that move's original Genre state, even if a later repair filled it.
Automated temporary-fixture repair tests cover all supported extensions, preservation,
stale findings, and write coordination. No manual Rekordbox import or Reload Tag
verification was performed for this feature; disposable-copy checks remain required.

## TODO

- [x] Add WAV and AIFF discovery, playback, comments/Genre, moves/undo, refresh,
  Trash, and read-only linting with lossless format-aware metadata handling.
- [ ] Manually verify Rekordbox import/Reload Tag for MP3, WAV, and AIFF comments
  and Genre, and evaluate international WAV INFO text encodings.
- [ ] Evaluate FLAC separately: Vorbis comments and picture blocks need a lossless
  preservation/undo strategy plus AVFoundation and Rekordbox checks. Not implemented.
- [ ] Evaluate M4A separately: MP4 metadata atoms (`©cmt`, `©gen`/`gnre`), artwork,
  unknown/DJ atoms, and chunk-offset-safe writes need separate design and tests.
  AAC versus ALAC playback/tag compatibility must be checked independently.
  Neither FLAC nor M4A support is approved or included in this change.
- [x] Add **Fix selected missing Genres…** to the destination tag checker with
  explicit per-file/Genre confirmation, nested-folder support, stale path/mapping/tag
  revalidation, lossless writes, mutation coordination, per-file reporting, and rescan.
  Missing tags only; mismatches, conflicts, malformed/unreadable metadata, and missing
  directories remain report-only. No repair undo; move/Genre undo history is unchanged.

- [x] Cut Bars editor: manual BPM/downbeat grid, zoom to sample level, cut and
  duplicate whole bars, crossfaded splices, save as a new file.
- [ ] Automatic BPM and downbeat detection (beat_this) to pre-fill the grid.
- [x] Metronome click over editor playback to check the grid by ear; tap tempo.
- [ ] Persist grids and edits across launches.

## Tests

Run `.venv/bin/python -m unittest -v`. Tests use temporary files only. FFmpeg is
needed for real-audio packet-preservation, native playback, and MP3 edit tests;
those tests skip if unavailable. Native tests require Swift/macOS. Trash is mocked; tests never
send files to the real Trash or modify the user's music library.
