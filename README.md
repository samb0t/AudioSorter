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
- `rekordboxXML`: the rekordbox XML file the app keeps up to date with recorded
  grids and memory cues (see [Exporting to Rekordbox](#exporting-to-rekordbox));
  `~/Music/Audio Sorter/rekordbox.xml` when omitted. Also editable in Settings.

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
   put the playhead on it (or press **C** for the nearest beat), and press **⌘\\**
   (or **D**) to make it a downbeat. Then check bar lines late in the track. If they drift off the kicks, fine-tune the BPM (decimals
   are fine). Turn on **Click** (**K**) to hear a metronome on every beat, accented
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
   Once the downbeat is set, the save also records the edit's grid and cues for
   Rekordbox.
6. **Record the original** (**⌘⇧S**, **Save grid & cues**) when you want the
   original itself in Rekordbox with this grid and cues. See
   [Exporting to Rekordbox](#exporting-to-rekordbox).

**Memory cues.** The keys follow Rekordbox's: **C** moves the playhead to the nearest
beat (like CUE with Quantize on), and **M** stores a memory cue at the playhead. Cues
show as green markers on the waveform and the overview strip, with the count beside
the cue buttons. A stopped
playhead is already where your click or arrow key put it (snapped or ⌘-click free);
while playing, the cue snaps to the nearest beat when snapping is on. **⇧M** removes
the cue nearest the playhead, and **[** / **]** jump to the previous/next cue. Cue
changes share the undo stack with edits. A track holds at most 10 cues, the number
Rekordbox's MEMORY CUE panel keeps. Cues belong to the edited track and move with
it: a cut removes the cues inside it and shifts later ones; a duplicate shifts the
cues after it and does not copy the cues inside the repeated bars. Each cue is kept
as a bar and beat position, so tempo changes and nudges carry it along with the grid
(a cue placed on a beat stays on that beat), while setting the downbeat again (D)
keeps every cue at the same place in the audio.

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
| ⌘\\ or D | Downbeat at playhead (⌘\\ as in Rekordbox) |
| T | Tap tempo |
| K | Metronome click on/off (remembered) |
| , / . | Nudge grid 1 ms earlier/later; Shift: 10 ms |
| ⌥-drag | Slide the grid |
| I / O | Selection start/end at the nearest bar line |
| X or Delete | Cut selection |
| ⌘D | Duplicate selection |
| C | Playhead to the nearest beat (Rekordbox's CUE with Quantize) |
| M | Store a memory cue at the playhead (as in Rekordbox) |
| ⇧M | Remove the memory cue nearest the playhead |
| [ / ] | Jump to the previous/next memory cue |
| ⌘Z / ⌘⇧Z | Undo/redo edits and cue changes |
| P | Play into the selection |
| ⌘S | Save edit (and record its grid and cues once the downbeat is set) |
| ⌘⇧S | Save grid & cues: record the original's grid and cues for Rekordbox |
| Esc | Stop (or leave the BPM/bars field without applying) |

Grid, edits, cues, and undo history are remembered per track until the app quits, so
closing the editor never loses anything. Unsaved edits show as "• unsaved" in the
editor and as the dot in its close button. Quitting (or closing the main window)
with unsaved edits or cue changes lists the affected tracks and asks first, with
Cancel as the default. Grid-only changes (BPM, downbeat) don't count as unsaved. Edits
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
- **BPM tag.** Saved edits get the grid's BPM in their ID3 `TBPM` frame (for example
  `124` or `123.5`), added to existing tags or as a new tag. **Write BPM to tag**
  writes it to the original, using the same lossless writer as comments and Genre
  (audio and other tags unchanged). It is enabled only when the value would change.
  WAV BPM goes in an ID3 chunk, because RIFF INFO has no BPM field, and Rekordbox
  may ignore it (as it does WAV ID3 Genre). Rekordbox reads beat grids from its own
  analysis or an XML import, never from tags; see
  [Exporting to Rekordbox](#exporting-to-rekordbox).
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

## Exporting to Rekordbox

Audio Sorter is where a track's beat grid and memory cues get set; Rekordbox (7.0 or
later) imports them from a rekordbox XML file and is left for building playlists.

**What gets recorded.** Nothing is sent to Rekordbox until you record it, and only
once the downbeat is set (D), because Rekordbox's bar 1 comes from it:
- **⌘S** in Cut Bars saves an edit and records the *edit file*: the edited track's
  grid and its cues at save time. Without a downbeat the edit is still saved, just
  not recorded; open the edit, set the downbeat and press ⌘⇧S.
- **⌘⇧S** (**Save grid & cues**) records the *original*: its grid, plus its cues
  mapped back to where they play in the original. Cues in duplicated audio have no
  single place in the original, so they are left out and the status line says how
  many; save the edit to keep them. The audio file is not changed.

Records live in `~/Library/Application Support/Audio Sorter/grids.json`, keyed by
the file's path and replaced atomically, so they survive quitting. Opening a
recorded track in Cut Bars restores its grid and cues (until the app quits, a track's
in-memory session, including unsaved edits, takes precedence). Moving a file with
1–9, undoing the move (⌘Z), and moving it to Trash update its record; comment,
Genre and BPM tag writes don't touch the audio timeline, so records stay valid.

**File → Rekordbox Records…** lists every record. Files renamed or deleted outside
the app show as **Missing**, and trashed ones as **In Trash**, at the top in red;
they are never dropped silently, but they are left out of the XML. Put the file
back to make its record current again, or select it and **Forget selected…**. At
launch, the main window's status line mentions any missing records.

**The XML.** After every change the whole XML is regenerated from the records and
replaced atomically, including only files that exist. Change its location in
**Settings** (`rekordboxXML`). For each track it contains:
- Name, Artist, Album, Genre and Comments from the file's tags (RIFF INFO first for
  WAV, as Rekordbox reads it), falling back to the file name; Kind, Size, TotalTime,
  SampleRate and BitRate; and `Location` as a percent-encoded `file://localhost/`
  URL, so spaces, `&`, `#`, `%` and accented names survive.
- The BPM, to 0.01 BPM (Rekordbox's precision; the editor's exact grid is kept in
  the record), and one `TEMPO` at the first beat at or after 0 s. Its `Battito`
  (beat in the bar) puts beat 1 on the downbeat set in the editor; a pickup before
  bar 1 starts on beat 2, 3 or 4. The grid is computed from your downbeat, so it is
  exact there even after rounding the BPM.
- Memory cues (`POSITION_MARK` with `Num="-1"`), at most 10 per track.

Not exported: hot cues (never written), loops, cue names and colours, playlists,
key, rating, track colour, My Tags, variable tempo, and time signatures other than 4/4.

### One-time Rekordbox setup (7.x)

1. **Preferences → View → Layout**: tick **rekordbox xml** under Media Browser.
2. **Preferences → Advanced → Database → rekordbox xml → Imported Library**: choose the
   XML (by default `~/Music/Audio Sorter/rekordbox.xml`). It only needs setting once;
   the app rewrites the same file.
3. Leave **Auto Analysis** as you like it: importing with it on kept the XML grid.

### Importing

1. In the tree, click the refresh button next to **rekordbox xml** to reread the file.
2. Open **rekordbox xml → All Tracks**, select the tracks, right-click → **Import To
   Collection** (or drag them onto Collection).

**New tracks** are added with the grid and cues from the XML. **Tracks already in
the collection** make Rekordbox ask: "This track already exists in the current
collection. Do you want to load information in the tag of the library being
imported? Beat Grid and Key information will not be loaded if the track is set
locked out against analyzing." In the test:
- **Yes** replaces the track's data with the XML's: the grid and memory cues are
  updated, memory cues you added in Rekordbox are replaced, and **hot cues and the
  rating are wiped** (presumably colour and My Tags too), because the XML doesn't
  carry them.
- **No** leaves the track as it was.

So import each track once, after setting its grid here, and only answer Yes for a
track whose Rekordbox-only data (hot cues, rating, colour, tags) you are happy to
lose. Keep **Don't ask me again** unticked: the dialog is what protects tracks you
have worked on in Rekordbox. Back up first with **File → Library → Backup Library**.
If a file was moved after it was imported, Rekordbox shows the old entry as missing;
importing the new location may add a second entry rather than relocate the first.

### Keeping the grid

Rekordbox's manual warns that re-analysing a track (**Analyze Track**) overwrites an
adjusted beat grid. Once a track is right, turn on **Analysis Lock** (right-click the
track or the **Track** menu): locked tracks skip analysis and grid editing. Unlock a
track before re-importing a corrected grid, since locked tracks don't take the XML's
grid. If **Preferences → Analysis → CUE Analysis** sets cues during analysis, keep
**Prohibit overwriting CUEs** on. Rekordbox's MEMORY CUE panel keeps 10 cues per track,
which is why the editor stops at 10.

### Timing calibration

Rekordbox ignores MP3 gapless headers, so it places MP3 audio later than
AVFoundation (which the editor uses) does. `rekordbox.py` adds the difference to every
grid and cue position, in one table (`OFFSETS`), based on how the file starts:

| File | Correction (samples) | Status |
| --- | --- | --- |
| WAV, AIFF | 0 | Measured |
| MP3 with a LAME header (LAME, most stores) | +2304 (52 ms at 44.1 kHz) | Measured |
| MP3 with FFmpeg's header (the app's MP3 edits) | +1152 (26 ms) | Measured |
| MP3 without a gapless header | +576 (13 ms) | Measured |
| MP3 with a Xing header but no LAME tag | +1152 | Assumed |
| MP3 whose LAME tag checksum is wrong | +2304 | Assumed |

Measured in Rekordbox 7.2.19 with `calibrate.py`, on 44.1 kHz click tracks: the grid
lines, bar 1 and the memory cues of every measured type were then verified to land
on the clicks within 1 ms, with the pickup beat before bar 1 shown as beat 4. 48 kHz
MP3s use the same sample counts but were not measured. To recheck after a Rekordbox
update, run `.venv/bin/python calibrate.py` (writes click tracks and `calibration.xml`
to `/tmp/Audio Sorter Calibration`, never the music library), import the XML, and
see which deliberately offset cue lands on its click; `--verify` checks the table and
`--update` rewrites the same tracks to test re-importing. Remove the calibration
tracks from Rekordbox afterwards.

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
- [x] Persist recorded grids and memory cues across launches (Rekordbox records).
- [ ] Persist unsaved edits and undo history across launches.
- [x] Memory cues in the editor, and a rekordbox XML export of grids and memory cues,
  calibrated and verified in Rekordbox 7.2.19 for WAV, AIFF and three kinds of MP3.
- [ ] Measure the assumed MP3 cases (Xing without LAME tag, bad LAME checksum) and
  48 kHz MP3s; check a few real store-bought MP3s end to end.
- [ ] Optionally warn before exporting a grid for a track already imported, since
  re-importing wipes its hot cues and rating in Rekordbox.

## Tests

Run `.venv/bin/python -m unittest -v`. Tests use temporary files only, including the
record store (the native tests point `AUDIO_SORTER_SUPPORT` at a temporary folder)
and every rekordbox XML they write. FFmpeg is
needed for real-audio packet-preservation, native playback, and MP3 edit tests;
those tests skip if unavailable. Native tests require Swift/macOS. Trash is mocked; tests never
send files to the real Trash or modify the user's music library.
