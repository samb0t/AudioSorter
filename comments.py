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


def unsupported(message):
    raise ValueError("Unsupported metadata: " + message)


def synchsafe(value):
    return bytes((value >> shift) & 127 for shift in (21, 14, 7, 0))


def id3_size(data):
    if len(data) != 4 or any(b & 128 for b in data):
        unsupported("invalid ID3 size")
    return sum(b << shift for b, shift in zip(data, (21, 14, 7, 0)))


class TagBlock:
    """Strict ID3v2.3/v2.4 parser; preserve even unknown frames as raw bytes."""
    def __init__(self, data=b""):
        self.version = 4
        self.frames = []
        self.padding = b""
        if not data:
            return
        if len(data) < 10 or data[:3] != b"ID3":
            unsupported("invalid ID3 header")
        self.version = data[3]
        if self.version not in (3, 4) or data[4:6] != b"\0\0":
            unsupported("ID3 version, flags, extended header or unsynchronization")
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

    def bytes(self):
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
    comments = tags.getall("COMM")
    return next((c for c in comments if c.desc == ""), comments[0] if comments else None)


def read_comment(path):
    metadata = AudioMetadata(path)
    if metadata.wav:
        return metadata.info_text(b"ICMT")
    comment = primary_comment(tags_for(path))
    return "\n".join(comment.text) if comment else ""


def write_comment(path, text):
    metadata = AudioMetadata(path)
    if metadata.wav:
        metadata.info_text(b"ICMT")  # Refuse ambiguous old text rather than replacing it.
        metadata.set_info(b"ICMT", info_encode(text) if text else None)
        metadata.save(edit_info=True)
        return
    tags = metadata.block.tags()
    old = primary_comment(tags)
    # Preserve other COMM frames (e.g. iTunes normalization information).
    lang, desc = (old.lang, old.desc) if old else ("eng", "")
    replacement = metadata.block.encode(COMM(encoding=3, lang=lang, desc=desc, text=[text])) if text else None
    metadata.block.replace(b"COMM", replacement, old.HashKey if old else "COMM::eng")
    metadata.save(edit_id3=True)


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
    if "genre" in change:
        genre = change["genre"]
        if not isinstance(genre, str) or not genre.strip():
            raise ValueError("Genre must be a nonempty string")
        replacement = metadata.block.encode(TCON(encoding=3, text=[genre])) if not metadata.wav or old else None
        if metadata.wav:
            metadata.set_info(b"IGNR", info_encode(genre))
    else:
        restore = change["restore"]
        if metadata.wav:
            metadata.set_info(b"IGNR", base64.b64decode(restore["riff"], validate=True) if restore["riff"] is not None else None)
            restore = restore["id3"]
        replacement = (base64.b64decode(restore["raw"], validate=True) if "raw" in restore else metadata.block.encode(TCON(encoding=restore["encoding"], text=restore["text"]))) if restore is not None else None
    metadata.block.replace(b"TCON", replacement)
    metadata.save(edit_id3=not metadata.wav or old_raw is not None or replacement is not None, edit_info=metadata.wav, validate=validate)
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
        return actual + ["ID3: " + " | ".join(values)], "Conflicting WAV RIFF INFO / ID3 Genre (Rekordbox reads IGNR)"
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
    """Separate explicit write operation. No move, directory creation, or undo entry.

    configuration is a callable so settings are reloaded before each commit.
    """
    results = []
    for finding in findings:
        result = {"path": finding.get("path", ""), "genre": finding.get("expected", "")}
        try:
            if finding.get("repairable") is not True or finding.get("problem") != "Missing Genre" or not finding.get("snapshot"):
                raise StaleRepair("Finding is report-only")

            def validate():
                config = configuration()
                if Path(config["destinationRoot"]).expanduser().absolute() != Path(root).expanduser().absolute():
                    raise StaleRepair("Destination root changed")
                path = repair_path(root, finding, config["destinations"])
                if repair_snapshot(path) != finding["snapshot"]:
                    raise StaleRepair("File changed since check")
                actual, conflict = genre_values(path)
                if actual or conflict:
                    raise StaleRepair("Genre is no longer safely missing")
                return path

            path = validate()
            write_genre(path, {"genre": finding["expected"]}, validate=validate)
            result.update(status="success", detail="Genre filled")
        except (StaleRepair, FileNotFoundError) as error:
            result.update(status="skipped", detail=str(error))
        except Exception as error:
            result.update(status="failed", detail=str(error))
        results.append(result)
    return {"results": results}


def lint_destinations(root, destinations):
    """Read-only recursive check against the configured Genre mappings."""
    root = Path(root).expanduser()
    result = {"scanned": 0, "checkedFolders": 0, "skippedFolders": 0, "issues": []}

    def issue(path, expected, actual, problem, snapshot=None):
        result["issues"].append({
            "path": str(path.absolute()), "file": str(path.relative_to(root)),
            "expected": expected, "actual": actual, "problem": problem,
            "repairable": problem == "Missing Genre" and snapshot is not None,
            "snapshot": snapshot,
        })

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
                    snapshot = repair_snapshot(path)
                    actual, problem = genre_values(path)
                    if snapshot != repair_snapshot(path):
                        raise OSError("File changed during check")
                    if problem or actual != [expected]:
                        issue(path, expected, actual, problem or ("Missing Genre" if not actual else "Genre mismatch"), snapshot)
                except Exception as error:
                    issue(path, expected, [], f"Cannot read tags: {error}")
    return result


if __name__ == "__main__":
    try:
        action, path = sys.argv[1:3]
        if action == "read":
            print(json.dumps({"comment": read_comment(path)}))
        elif action == "write":
            write_comment(path, sys.stdin.read())
            print(json.dumps({"ok": True}))
        elif action == "write-genre":
            previous = write_genre(path, json.load(sys.stdin))
            print(json.dumps({"previous": previous}))
        elif action == "lint":
            print(json.dumps(lint_destinations(path, json.load(sys.stdin)["destinations"])))
        elif action == "repair-missing-genres":
            request = json.load(sys.stdin)
            def current_configuration():
                return json.loads(Path(request["configurationPath"]).read_text())
            print(json.dumps(repair_missing_genres(path, request["findings"], current_configuration)))
        else:
            raise ValueError("Unknown action")
    except Exception as error:
        print(json.dumps({"error": str(error)}))
        sys.exit(1)
