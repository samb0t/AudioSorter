#!/bin/zsh
# Regenerates requirements-release.lock: every package the release app installs, pinned to
# an exact version with its SHA-256 hashes. Run after changing requirements*.txt:
#
#     zsh tools/lock_deps.sh            # keep the other pins, resolve what changed
#     zsh tools/lock_deps.sh --upgrade  # move every unpinned dependency to its latest
#
# Resolves with python3, which must match the release build: 3.12 on Apple Silicon.
set -eu
ROOT="${0:A:h:h}"
if [ "$(python3 -c 'import platform, sys; print(sys.version_info[:2] == (3, 12) and platform.machine())')" != arm64 ]; then
  echo "python3 must be Python 3.12 on Apple Silicon, like the release build." >&2
  exit 1
fi
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/python" -m pip install --quiet --disable-pip-version-check pip-tools==7.7.0
cd "$ROOT"
CUSTOM_COMPILE_COMMAND="zsh tools/lock_deps.sh" \
  "$WORK/venv/bin/pip-compile" --quiet --generate-hashes --allow-unsafe --strip-extras --no-emit-index-url \
  --output-file requirements-release.lock "$@" requirements.txt requirements-detect.txt
echo "Wrote requirements-release.lock"
