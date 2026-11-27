"""Format-aware, lossless tag edits. Invoked by the native app.

Untouched ID3 frames and RIFF/IFF chunks are copied verbatim, not reserialized.
Ambiguous/unsupported layouts fail closed, including on read-only lint scans.
"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from mutagen.id3 import COMM, Frames, ID3, TCON

EXTENSIONS = {".mp3", ".wav", ".aif", ".aiff"}
WAV_GENRE_CONFLICT = "Conflicting WAV RIFF INFO / ID3 Genre (Rekordbox reads IGNR)"


def unsupported(message):
    raise ValueError("Unsupported metadata: " + message)


def synchsafe(value):
    return bytes((value >> shift) & 127 for shift in (21, 14, 7, 0))


def id3_size(data):
    if len(data) != 4 or any(b & 128 for b in data):
        unsupported("invalid ID3 size")
    return sum(b << shift for b, shift in zip(data, (21, 14, 7, 0)))


def header_problem(version, revision, flags):
    """Why an ID3v2 header can't be handled, or None."""
    if version not in (2, 3, 4):
        return f"ID3v2.{version} tag"
    if revision:
        return "ID3 tag with a minor revision"
    names = {0x80: "unsynchronization", 0x40: "compression" if version == 2 else "an extended header",
             0x20: "the experimental flag", 0x10: "a footer"}
    used = [name for bit, name in names.items() if flags & bit] + (["unknown header flags"] if flags & 0x0F else [])
    return "ID3 tag uses " + " and ".join(used) if used else None


class TagBlock:
    """Strict ID3v2.3/v2.4 parser; preserve even unknown frames as raw bytes.

    An ID3v2.2 tag (older iTunes) has three-letter frames that can't be edited in place, so it
    is converted to v2.3 when read, and only if every frame converts without loss. A write then
    saves the v2.3 tag; `upgraded_from` keeps the original bytes for undo.
    """
    def __init__(self, data=b""):
        self.version = 4
        self.frames = []
        self.padding = b""
        self.upgraded_from = None
        self.verbatim = None  # Exact bytes to write instead, when undo restores a v2.2 tag.
        if not data:
            return
        if len(data) < 10 or data[:3] != b"ID3":
            unsupported("invalid ID3 header")
        problem = header_problem(data[3], data[4], data[5])
        if problem:
            unsupported(problem)
        if data[3] == 2:
            self.upgrade(data)
            return
        self.version = data[3]
        if id3_size(data[6:10]) != len(data) - 10:
            unsupported("ID3 length/trailing bytes")
        pos, identities = 10, set()
        while pos < len(data):
            if data[pos] == 0:
                self.padding = data[pos:]
                if any(self.padding):
                    unsupported("nonzero ID3 padding")
                break
            header = data[pos:pos + 10]
            if (len(header) != 10 or header[0] not in b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                    or any(c not in b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for c in header[:4])):
                unsupported("invalid ID3 frame header")
            size = id3_size(header[4:8]) if self.version == 4 else int.from_bytes(header[4:8], "big")
            end = pos + 10 + size
            if not size or end > len(data) or header[8:10] != b"\0\0":
                unsupported("truncated, compressed, encrypted or flagged ID3 frame")
            raw = data[pos:end]
            # Mutagen decodes identities; it never rewrites untouched frames.
            decoded = ID3(fileobj=io.BytesIO(b"ID3" + bytes([self.version, 0, 0]) + synchsafe(len(raw)) + raw), translate=False)
            identity = next(iter(decoded), header[:4].decode("ascii"))
            if identity in identities or (header[:4] == b"TCON" and any(f[:4] == b"TCON" for f in self.frames)):
                unsupported("duplicate ID3 frame " + identity)
            if header[:4].decode("ascii") in Frames and not decoded:
                unsupported("undecodable ID3 frame")
            identities.add(identity)
            self.frames.append(raw)
            pos = end

    def upgrade(self, data):
        if id3_size(data[6:10]) != len(data) - 10:
            unsupported("ID3 length/trailing bytes")
        pos, count = 10, 0
        while pos < len(data) and data[pos] != 0:
            header = data[pos:pos + 6]
            if len(header) != 6 or any(c not in b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for c in header[:3]):
                unsupported("invalid ID3v2.2 frame header")
            size = int.from_bytes(header[3:6], "big")
            if not size or pos + 6 + size > len(data):
                unsupported("truncated ID3v2.2 frame")
            count += 1
            pos += 6 + size
        if any(data[pos:]):
            unsupported("nonzero ID3 padding")
        # Mutagen reads v2.2 frames as their v2.3 equivalents, but drops any it doesn't know,
        # and keys collapse duplicates: either way the count changes.
        original = ID3(fileobj=io.BytesIO(data), translate=False)
        if original.unknown_frames or len(original) != count:
            unsupported("ID3v2.2 tag with frames that can't be converted to v2.3 without loss")
        original.update_to_v23()
        stream = io.BytesIO()
        original.save(stream, v2_version=3, padding=lambda _: 0)
        converted = TagBlock(stream.getvalue())
        # Every frame must read back the same. Only artwork's format field changes form: v2.2
        # names it ("PNG"), v2.3 gives a MIME type ("image/png").
        def values(tags):
            return {key: {name: value for name, value in vars(frame).items() if name != "mime"} for key, frame in tags.items()}
        if values(converted.tags()) != values(ID3(fileobj=io.BytesIO(data), translate=False)):
            unsupported("ID3v2.2 tag that reads differently after conversion to v2.3")
        self.version, self.frames = 3, converted.frames
        # Same total size where it fits, so the audio stays where it was.
        self.padding = b"\0" * max(len(data) - len(converted.bytes()), 0)
        self.upgraded_from = data

    def bytes(self):
        if self.verbatim is not None:
            return self.verbatim
        body = b"".join(self.frames) + self.padding
        return b"ID3" + bytes([self.version, 0, 0]) + synchsafe(len(body)) + body

    def tags(self):
        return ID3(fileobj=io.BytesIO(self.bytes()), translate=False)

    def replace(self, identifier, replacement, identity=None):
        retained = []
        for frame in self.frames:
            matches = frame[:4] == identifier
            if matches and identity is not None:
                block = TagBlock(b"ID3" + bytes([self.version, 0, 0]) + synchsafe(len(frame)) + frame)
                matches = identity in block.tags()
            if not matches:
                retained.append(frame)
        if replacement is not None:
            retained.append(replacement)
        self.frames = retained

    def encode(self, frame):
        tags = ID3()
        tags.add(frame)
        stream = io.BytesIO()
        tags.save(stream, v2_version=self.version, padding=lambda _: 0)
        return TagBlock(stream.getvalue()).frames[0]


def chunks(data, endian):
    result, pos = [], 0
    while pos < len(data):
        if pos + 8 > len(data):
            unsupported("truncated chunk header")
        size = int.from_bytes(data[pos + 4:pos + 8], endian)
        end = pos + 8 + size + (size & 1)
        if end > len(data):
            unsupported("truncated chunk or missing alignment byte")
        result.append((data[pos:pos + 4], data[pos + 8:pos + 8 + size], data[pos:end]))
        pos = end
    return result


def chunk(identifier, payload, endian):
    return identifier + len(payload).to_bytes(4, endian) + payload + (b"\0" if len(payload) & 1 else b"")


class AudioMetadata:
    def __init__(self, path):
        self.path = Path(path)
        if self.path.is_symlink() or not self.path.is_file():
            unsupported("not a regular file (or symbolic link)")
        self.original = self.path.read_bytes()
        self.extension = self.path.suffix.lower()
        if self.extension not in EXTENSIONS:
            unsupported("audio format")
        self.wav = self.extension == ".wav"
        self.container = self.extension != ".mp3"
        self.info = []
        self.info_index = self.id3_index = None
        self.id3_present = False
        if not self.container:
            length = 0
            if self.original.startswith(b"ID3"):
                length = 10 + id3_size(self.original[6:10])
                if length > len(self.original):
                    unsupported("truncated ID3 tag")
            self.block = TagBlock(self.original[:length])
            self.audio = self.original[length:]
            self.id3_present = bool(length)
            return
        self.endian = "little" if self.wav else "big"
        data = self.original
        if len(data) < 12 or data[:4] != (b"RIFF" if self.wav else b"FORM") or data[8:12] not in ((b"WAVE",) if self.wav else (b"AIFF", b"AIFC")):
            unsupported("expected RIFF WAVE or FORM AIFF/AIFC (RF64/RIFX unsupported)")
        if int.from_bytes(data[4:8], self.endian) != len(data) - 8:
            unsupported("container size/trailing bytes")
        self.parts = chunks(data[12:], self.endian)
        self.block = TagBlock()
        for i, (key, payload, _) in enumerate(self.parts):
            if key.lower() == b"id3 ":
                if self.id3_index is not None:
                    unsupported("duplicate ID3 chunk")
                if not payload:
                    unsupported("empty ID3 chunk")
                self.id3_index = i
                self.id3_present = True
                self.block = TagBlock(payload)
            if self.wav and key == b"LIST":
                if len(payload) < 4:
                    unsupported("short LIST chunk")
                if payload[:4] == b"INFO":
                    if self.info_index is not None:
                        unsupported("duplicate LIST INFO")
                    self.info_index = i
                    self.info = chunks(payload[4:], "little")
                    keys = [entry[0] for entry in self.info]
                    if len(keys) != len(set(keys)):
                        unsupported("duplicate INFO field")
        required = (b"fmt ", b"data") if self.wav else (b"COMM", b"SSND")
        if any(sum(key == r for key, _, _ in self.parts) != 1 for r in required):
            unsupported("missing or duplicate audio chunks")

    def info_value(self, key):
        return next((payload for name, payload, _ in self.info if name == key), None)

    def info_text(self, key):
        payload = self.info_value(key)
        if payload is None:
            return ""
        if not payload.endswith(b"\0") or b"\0" in payload[:-1]:
            unsupported("INFO text must have one terminating NUL")
        try:
            return payload[:-1].decode("ascii")
        except UnicodeDecodeError:
            unsupported("non-ASCII INFO text has ambiguous encoding")

    def set_info(self, key, payload):
        updated = []
        found = False
        for name, value, raw in self.info:
            if name == key:
                found = True
                if payload is not None:
                    updated.append((key, payload, chunk(key, payload, "little")))
            else:
                updated.append((name, value, raw))
        if not found and payload is not None:
            updated.append((key, payload, chunk(key, payload, "little")))
        self.info = updated

    def output(self, edit_id3=False, edit_info=False):
        if not self.container:
            return self.block.bytes() + self.audio
        replacements = {}
        additions = []
        for enabled, index, key, payload in (
            (edit_id3, self.id3_index, b"id3 " if self.wav else b"ID3 ", self.block.bytes()),
            (edit_info, self.info_index, b"LIST", b"INFO" + b"".join(raw for _, _, raw in self.info)),
        ):
            if enabled:
                raw = chunk(self.parts[index][0] if index is not None else key, payload, self.endian)
                if index is None:
                    additions.append(raw)
                else:
                    replacements[index] = raw
        body = self.original[8:12] + b"".join(replacements.get(i, raw) for i, (_, _, raw) in enumerate(self.parts)) + b"".join(additions)
        return self.original[:4] + len(body).to_bytes(4, self.endian) + body

    def save(self, edit_id3=False, edit_info=False, validate=None):
        output = self.output(edit_id3, edit_info)
        fd, temporary = tempfile.mkstemp(prefix=".audio-sorter-", suffix=self.extension, dir=self.path.parent)
        os.close(fd)
        try:
            shutil.copy2(self.path, temporary)
            with open(temporary, "wb") as stream:
                stream.write(output)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, self.path.stat().st_mode)
            verified = AudioMetadata(temporary)
            if verified.original != output:
                raise OSError("Temporary file verification failed")
            if self.path.is_symlink() or self.path.read_bytes() != self.original:
                raise OSError("Source changed during tag write")
            if validate is not None:
                validate()
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def tags_for(path):
    return AudioMetadata(path).block.tags()


def primary_comment(tags):
    # iTunes keeps playback data in described COMM frames (iTunNORM, iTunSMPB, iTunPGAP,
    # iTunes_CDDB_IDs); they are never the user's comment, so never shown or replaced.
    comments = [c for c in tags.getall("COMM") if not c.desc.startswith("iTun")]
    return next((c for c in comments if c.desc == ""), comments[0] if comments else None)


def read_comment(path):
    metadata = AudioMetadata(path)
    if metadata.wav:
        return metadata.info_text(b"ICMT")
    comment = primary_comment(tags_for(path))
    return "\n".join(comment.text) if comment else ""


def write_comment(path, text):
    """Returns True when the file's ID3v2.2 tag was upgraded to v2.3 to make the edit."""
    metadata = AudioMetadata(path)
    if metadata.wav:
        metadata.info_text(b"ICMT")  # Refuse ambiguous old text rather than replacing it.
        metadata.set_info(b"ICMT", info_encode(text) if text else None)
        metadata.save(edit_info=True)
        return False
    tags = metadata.block.tags()
    old = primary_comment(tags)
    # Preserve other COMM frames (e.g. iTunes normalization information).
    lang, desc = (old.lang, old.desc) if old else ("eng", "")
    replacement = metadata.block.encode(COMM(encoding=3, lang=lang, desc=desc, text=[text])) if text else None
    metadata.block.replace(b"COMM", replacement, old.HashKey if old else "COMM::eng")
    metadata.save(edit_id3=True)
    return metadata.block.upgraded_from is not None


def info_encode(text):
    # INFO has no universal Unicode encoding. Do not guess Rekordbox's codepage.
    if "\0" in text:
        raise ValueError("Embedded NUL is not supported")
    try:
        return text.encode("ascii") + b"\0"
    except UnicodeEncodeError:
        raise ValueError("WAV INFO supports ASCII edits only; Unicode/codepage compatibility needs Rekordbox verification")


def write_genre(path, change, validate=None):
    """Set a genre or restore its prior frame; return a JSON-safe undo snapshot."""
    metadata = AudioMetadata(path)
    tags = metadata.block.tags()
    old = tags.get("TCON")
    previous = {"text": list(old.text), "encoding": int(old.encoding)} if old else None
    old_raw = next((frame for frame in metadata.block.frames if frame[:4] == b"TCON"), None)
    if previous is not None:
        previous["raw"] = base64.b64encode(old_raw).decode("ascii")
    if metadata.wav:
        metadata.info_text(b"IGNR")
        previous = {"riff": base64.b64encode(metadata.info_value(b"IGNR")).decode("ascii") if metadata.info_value(b"IGNR") is not None else None, "id3": previous}
    exact = None
    if "genre" in change:
        genre = change["genre"]
        if not isinstance(genre, str) or not genre.strip():
            raise ValueError("Genre must be a nonempty string")
        replacement = metadata.block.encode(TCON(encoding=3, text=[genre])) if not metadata.wav or old else None
        if metadata.wav:
            metadata.set_info(b"IGNR", info_encode(genre))
    else:
        restore = change["restore"]
        if isinstance(restore, dict) and "upgradedFrom" in restore:
            # Put the original ID3v2.2 tag back if nothing has changed the tag since; otherwise
            # restore only the Genre, in the upgraded tag, so later edits survive.
            if hashlib.sha256(metadata.block.bytes()).hexdigest() == restore["upgradedTag"]:
                exact = base64.b64decode(restore["upgradedFrom"], validate=True)
            restore = restore["previous"]
        if metadata.wav:
            metadata.set_info(b"IGNR", base64.b64decode(restore["riff"], validate=True) if restore["riff"] is not None else None)
            restore = restore["id3"]
        replacement = (base64.b64decode(restore["raw"], validate=True) if "raw" in restore else metadata.block.encode(TCON(encoding=restore["encoding"], text=restore["text"]))) if restore is not None else None
    metadata.block.replace(b"TCON", replacement)
    metadata.block.verbatim = exact
    edit_id3 = not metadata.wav or old_raw is not None or replacement is not None or exact is not None
    metadata.save(edit_id3=edit_id3, edit_info=metadata.wav, validate=validate)
    if edit_id3 and exact is None and metadata.block.upgraded_from is not None:
        previous = {"upgradedFrom": base64.b64encode(metadata.block.upgraded_from).decode("ascii"),
                    "upgradedTag": hashlib.sha256(metadata.block.bytes()).hexdigest(), "previous": previous}
    return previous


def genre_values(path):
    metadata = AudioMetadata(path)
    values = [str(value) for frame in tags_for(path).getall("TCON") for value in frame.text]
    nonempty = any(value != "" for value in values)
    if not metadata.wav:
        return values if nonempty else [], None
    metadata.info_text(b"ICMT")  # Malformed supported metadata remains report-only.
    riff = metadata.info_text(b"IGNR")
    actual = [riff] if riff else []
    if nonempty and values != actual:
        return actual + ["ID3: " + " | ".join(values)], WAV_GENRE_CONFLICT
    return actual, None


def repair_snapshot(path):
    stat = path.stat()
    return {"device": stat.st_dev, "inode": stat.st_ino, "mtime": stat.st_mtime_ns,
            "size": stat.st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


class StaleRepair(ValueError):
    pass


def repair_path(root, finding, destinations):
    """Validate containment without following any link below the configured root."""
    root = Path(root).expanduser().absolute()
    path = Path(finding["path"])
    if root.is_symlink() or not root.is_dir() or not path.is_absolute():
        raise StaleRepair("Destination is missing or a symbolic link")
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise StaleRepair("Path outside configured destination")
    if len(relative.parts) < 2 or any(p in ("..", ".") or p.startswith(".") for p in relative.parts):
        raise StaleRepair("Invalid destination path")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise StaleRepair("Symbolic link in destination path")
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise StaleRepair("File missing or outside destination")
    matches = [d for d in destinations if isinstance(d, dict) and d.get("directory") == relative.parts[0]]
    if len(matches) != 1 or not matches[0].get("genre") or matches[0]["genre"] != finding.get("expected"):
        raise StaleRepair("Destination Genre mapping changed")
    return path


def repair_missing_genres(root, findings, configuration):
    """Legacy missing-only operation; never overwrite a nonempty genre."""
    return repair_genres(root, findings, configuration, include_wav_conflicts=False)


def repair_genres(root, findings, configuration, *, include_wav_conflicts=True):
    """Separate explicit write operation. No move, directory creation, or undo entry.

    configuration is a callable so settings are reloaded before each commit.
    """
    results = []
    for finding in findings:
        result = {"path": finding.get("path", ""), "genre": finding.get("expected", "")}
        try:
            problem = finding.get("problem")
            is_conflict = include_wav_conflicts and problem == WAV_GENRE_CONFLICT
            if finding.get("repairable") is not True or (problem != "Missing Genre" and not is_conflict) or not finding.get("snapshot"):
                raise StaleRepair("Finding is report-only")

            def validate():
                config = configuration()
                if Path(config["destinationRoot"]).expanduser().absolute() != Path(root).expanduser().absolute():
                    raise StaleRepair("Destination root changed")
                path = repair_path(root, finding, config["destinations"])
                if repair_snapshot(path) != finding["snapshot"]:
                    raise StaleRepair("File changed since check")
                actual, conflict = genre_values(path)
                if is_conflict:
                    if path.suffix.lower() != ".wav" or conflict != WAV_GENRE_CONFLICT or actual != finding.get("actual"):
                        raise StaleRepair("WAV Genre conflict changed since check")
                elif actual or conflict:
                    raise StaleRepair("Genre is no longer safely missing")
                return path

            path = validate()
            write_genre(path, {"genre": finding["expected"]}, validate=validate)
            result.update(status="success", detail="WAV RIFF INFO and ID3 Genres synchronized" if is_conflict else "Genre filled")
        except (StaleRepair, FileNotFoundError) as error:
            result.update(status="skipped", detail=str(error))
        except Exception as error:
            result.update(status="failed", detail=str(error))
        results.append(result)
    return {"results": results}


def genre_issue(root, path, expected, actual, problem, snapshot=None):
    return {
        "path": str(path.absolute()), "file": str(path.relative_to(root)),
        "expected": expected, "actual": actual, "problem": problem,
        "repairable": problem in ("Missing Genre", WAV_GENRE_CONFLICT) and snapshot is not None,
        "snapshot": snapshot,
    }


def lint_file(root, path, expected):
    """Return a fresh finding, or None only after a verified clean read."""
    try:
        snapshot = repair_snapshot(path)
        actual, problem = genre_values(path)
        if snapshot != repair_snapshot(path):
            raise OSError("File changed during check")
        if problem or actual != [expected]:
            return genre_issue(root, path, expected, actual,
                               problem or ("Missing Genre" if not actual else "Genre mismatch"), snapshot)
    except Exception as error:
        return genre_issue(root, path, expected, [], f"Cannot read tags: {error}")


def recheck_genres(root, findings, configuration):
    """Read only the attempted paths; never enumerate destination folders."""
    root = Path(root).expanduser().absolute()
    config = configuration()
    if Path(config["destinationRoot"]).expanduser().absolute() != root:
        raise StaleRepair("Destination root changed; use Check again for a full scan")
    result = {"checkedPaths": [], "issues": []}
    for finding in findings:
        path = Path(finding["path"])
        expected = finding.get("expected", "")
        try:
            relative = path.relative_to(root)
            matches = [d for d in config["destinations"] if isinstance(d, dict)
                       and relative.parts and d.get("directory") == relative.parts[0]]
            if len(matches) != 1 or not matches[0].get("genre"):
                raise StaleRepair("Genre mapping changed; use Check again for a full scan")
            expected = matches[0]["genre"]
            repair_path(root, dict(finding, expected=expected), config["destinations"])
            issue = lint_file(root, path, expected)
        except Exception as error:
            # Preserve even removed or unsafe paths as report-only findings.
            issue = dict(finding, expected=expected, actual=[], repairable=False,
                         snapshot=None, problem=f"Cannot recheck file: {error}")
        result["checkedPaths"].append(str(path))
        if issue is not None:
            result["issues"].append(issue)
    return result


def lint_destinations(root, destinations):
    """Read-only recursive check against the configured Genre mappings."""
    root = Path(root).expanduser()
    result = {"scanned": 0, "checkedFolders": 0, "skippedFolders": 0, "issues": []}

    def issue(path, expected, actual, problem, snapshot=None):
        result["issues"].append(genre_issue(root, path, expected, actual, problem, snapshot))

    for destination in destinations:
        if isinstance(destination, str) or not destination.get("genre"):
            result["skippedFolders"] += 1
            continue
        name, expected = destination["directory"], destination["genre"]
        if not name or name in (".", "..") or "/" in name:
            raise ValueError("Invalid destination directory")
        folder = root / name
        result["checkedFolders"] += 1
        if root.is_symlink() or folder.is_symlink() or not folder.is_dir():
            issue(folder, expected, [], "Destination is missing, not a directory, or a symbolic link")
            continue

        def walk_error(error):
            issue(Path(error.filename), expected, [], f"Cannot scan directory: {error}")

        for directory, subdirectories, filenames in os.walk(folder, followlinks=False, onerror=walk_error):
            directory = Path(directory)
            subdirectories[:] = sorted(name for name in subdirectories
                                       if not name.startswith(".") and not (directory / name).is_symlink())
            for filename in sorted(filenames):
                path = directory / filename
                if filename.startswith(".") or path.suffix.lower() not in EXTENSIONS or path.is_symlink():
                    continue
                try:
                    if not path.is_file():
                        continue
                    result["scanned"] += 1
                    finding = lint_file(root, path, expected)
                    if finding is not None:
                        result["issues"].append(finding)
                except Exception as error:
                    issue(path, expected, [], f"Cannot read tags: {error}")
    return result


if __name__ == "__main__":
    try:
        action, path = sys.argv[1:3]
        if action == "read":
            print(json.dumps({"comment": read_comment(path)}))
        elif action == "read-genre":
            genres, warning = genre_values(path)
            print(json.dumps({"genres": genres, "warning": warning}))
        elif action == "write":
            print(json.dumps({"ok": True, "upgradedID3": write_comment(path, sys.stdin.read())}))
        elif action == "write-genre":
            previous = write_genre(path, json.load(sys.stdin))
            print(json.dumps({"previous": previous}))
        elif action == "read-bpm":
            from edit import read_bpm
            print(json.dumps({"bpm": read_bpm(path)}))
        elif action == "write-bpm":
            from edit import write_bpm
            print(json.dumps({"ok": True, "upgradedID3": write_bpm(path, json.load(sys.stdin)["bpm"])}))
        elif action == "cut":
            from edit import cut_file
            print(json.dumps(cut_file(path, json.load(sys.stdin))))
        elif action == "detect":
            from detect import detect
            print(json.dumps(detect(path, json.load(sys.stdin)["model"])))
        elif action == "rekordbox-xml":
            from rekordbox import export
            print(json.dumps(export(path, json.load(sys.stdin)["output"])))
        elif action == "lint":
            print(json.dumps(lint_destinations(path, json.load(sys.stdin)["destinations"])))
        elif action in ("repair-missing-genres", "repair-genres", "recheck-genres"):
            request = json.load(sys.stdin)
            def current_configuration():
                return json.loads(Path(request["configurationPath"]).read_text())
            repair = {"repair-missing-genres": repair_missing_genres,
                      "repair-genres": repair_genres, "recheck-genres": recheck_genres}[action]
            print(json.dumps(repair(path, request["findings"], current_configuration)))
        else:
            raise ValueError("Unknown action")
    except Exception as error:
        print(json.dumps({"error": str(error)}))
        sys.exit(1)
