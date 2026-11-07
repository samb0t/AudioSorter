"""Read/write ID3 comments without re-encoding audio. Invoked by the native app."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from mutagen.id3 import COMM, ID3, ID3NoHeaderError


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


if __name__ == "__main__":
    try:
        action, path = sys.argv[1:3]
        if action == "read":
            print(json.dumps({"comment": read_comment(path)}))
        elif action == "write":
            write_comment(path, sys.stdin.read())
            print(json.dumps({"ok": True}))
        else:
            raise ValueError("Unknown action")
    except Exception as error:
        print(json.dumps({"error": str(error)}))
        sys.exit(1)
