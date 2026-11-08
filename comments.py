"""Read/write ID3 comments without re-encoding audio. Invoked by the native app."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from mutagen.id3 import COMM, ID3, ID3NoHeaderError, TCON


def tags_for(path):
    try:
        return ID3(path)
    except ID3NoHeaderError:
        return ID3()


def primary_comment(tags):
    comments = tags.getall("COMM")
    return next((c for c in comments if c.desc == ""), comments[0] if comments else None)


def read_comment(path):
    comment = primary_comment(tags_for(path))
    return "\n".join(comment.text) if comment else ""


def write_comment(path, text):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Refusing to replace a symbolic link")
    tags = tags_for(path)
    old = primary_comment(tags)
    # Preserve other COMM frames (e.g. iTunes normalization information).
    lang, desc = (old.lang, old.desc) if old else ("eng", "")
    if old:
        del tags[old.HashKey]
    if text:
        tags.add(COMM(encoding=3, lang=lang, desc=desc, text=[text]))
    save_tags(path, tags)


def write_genre(path, change):
    """Set a genre or restore its prior frame; return a JSON-safe undo snapshot."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Refusing to replace a symbolic link")
    tags = tags_for(path)
    old = tags.get("TCON")
    previous = {"text": list(old.text), "encoding": int(old.encoding)} if old else None
    if "genre" in change:
        genre = change["genre"]
        if not isinstance(genre, str) or not genre.strip():
            raise ValueError("Genre must be a nonempty string")
        replacement = TCON(encoding=3, text=[genre])
    else:
        restore = change["restore"]
        replacement = TCON(encoding=restore["encoding"], text=restore["text"]) if restore is not None else None
    tags.delall("TCON")
    if replacement is not None:
        tags.add(replacement)
    save_tags(path, tags)
    return previous


def save_tags(path, tags):
    version = 3 if tags.version[1] == 3 else 4
    fd, temporary = tempfile.mkstemp(prefix=".audio-sorter-", suffix=".mp3", dir=path.parent)
    os.close(fd)
    try:
        shutil.copy2(path, temporary)
        tags.save(temporary, v2_version=version)
        # Same-directory replacement keeps the original intact if tag writing fails.
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def lint_destinations(root, destinations):
    """Read-only recursive check against the configured Genre mappings."""
    root = Path(root).expanduser()
    result = {"scanned": 0, "checkedFolders": 0, "skippedFolders": 0, "issues": []}

    def issue(path, expected, actual, problem):
        result["issues"].append({
            "path": str(path.absolute()), "file": str(path.relative_to(root)),
            "expected": expected, "actual": actual, "problem": problem,
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
        if folder.is_symlink() or not folder.is_dir():
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
                if filename.startswith(".") or path.suffix.lower() != ".mp3" or path.is_symlink():
                    continue
                try:
                    if not path.is_file():
                        continue
                    result["scanned"] += 1
                    actual = [str(value) for frame in tags_for(path).getall("TCON") for value in frame.text]
                    if actual != [expected]:
                        issue(path, expected, actual, "Missing Genre" if not actual else "Genre mismatch")
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
        else:
            raise ValueError("Unknown action")
    except Exception as error:
        print(json.dumps({"error": str(error)}))
        sys.exit(1)
