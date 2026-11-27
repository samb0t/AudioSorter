#!/bin/zsh
set -eu
ROOT="${0:A:h}"
SOURCES="$ROOT/app"
# Plain builds run from this folder, using its .venv, config.json and (with --detect) the
# beat_this model. --release builds a self-contained app in .build/release with its own
# Python and model; it keeps its settings in ~/Library/Application Support/Audio Sorter.
DETECT=0
RELEASE=0
for ARG in "$@"; do
  case "$ARG" in
    --detect) DETECT=1 ;;
    --release) RELEASE=1; DETECT=1 ;;
    *) echo "Unknown option: $ARG (use --detect or --release)" >&2; exit 2 ;;
  esac
done
VERSION="${AUDIO_SORTER_VERSION:-0.9.0}"
BUILD_NUMBER="${AUDIO_SORTER_BUILD:-1}"
# "-" signs ad hoc; a Developer ID identity also turns on the hardened runtime for notarizing.
IDENTITY="${AUDIO_SORTER_SIGN_IDENTITY:--}"
SIGN_FLAGS=(--force --sign "$IDENTITY")
[ "$IDENTITY" = "-" ] || SIGN_FLAGS+=(--options runtime --timestamp)
MINIMUM_MACOS=14.0

MODEL="$ROOT/.build/beat_this/final0.ckpt"
MODEL_SHA256=8c328b45f59d8dd3dff219253ff6a8d6482be57d0133a29140e2febbf8eb8331
# Relocatable CPython for release builds: https://github.com/astral-sh/python-build-standalone
PYTHON_ARCHIVE="$ROOT/.build/downloads/cpython-3.12.16+20261124-aarch64-apple-darwin-install_only_stripped.tar.gz"
PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20261124/cpython-3.12.16%2B20261124-aarch64-apple-darwin-install_only_stripped.tar.gz"
PYTHON_SHA256=9d6d9d0d85feda3f9cb4eb3e048f0fed21ed280feda087927eaa4fbc7124826f

# Downloads once into .build, verified against a pinned checksum.
fetch() {
  local url=$1 file=$2 sha=$3
  [ -f "$file" ] && return
  mkdir -p "${file:h}"
  curl -fL --progress-bar -o "$file.part" "$url"
  if [ "$(shasum -a 256 "$file.part" | cut -d' ' -f1)" != "$sha" ]; then
    rm -f "$file.part"
    echo "The download of ${file:t} failed its checksum." >&2
    exit 1
  fi
  mv "$file.part" "$file"
}

if [ $DETECT = 1 ]; then
  fetch "https://cloud.cp.jku.at/public.php/dav/files/7ik4RrBKTS273gp/final0.ckpt" "$MODEL" "$MODEL_SHA256"
fi

if [ $RELEASE = 1 ]; then
  APP="$ROOT/.build/release/Audio Sorter.app"
  rm -rf "$APP"
else
  APP="$ROOT/Audio Sorter.app"
fi
RESOURCES="$APP/Contents/Resources"
mkdir -p "$APP/Contents/MacOS" "$RESOURCES"

if [ $RELEASE = 1 ]; then
  fetch "$PYTHON_URL" "$PYTHON_ARCHIVE" "$PYTHON_SHA256"
  tar -xzf "$PYTHON_ARCHIVE" -C "$RESOURCES"
  PYTHON="$RESOURCES/python/bin/python3"
  # No bytecode while building: it would record this folder's path. compileall adds it below.
  export PYTHONDONTWRITEBYTECODE=1
  "$PYTHON" -m pip install --disable-pip-version-check --no-warn-script-location --no-compile \
    -r "$ROOT/requirements.txt" -r "$ROOT/requirements-detect.txt"
  "$PYTHON" -m pip uninstall --disable-pip-version-check -y pip
  # Drop what the helper never uses. Console scripts go too: their #! lines point at this
  # build folder.
  STDLIB="$RESOURCES/python/lib/python3.12"
  rm -rf "$STDLIB"/{test,idlelib,tkinter,turtledemo,ensurepip} "$STDLIB"/lib-dynload/_tkinter*.so \
    "$RESOURCES/python/lib"/{tcl*,tk*,itcl*,thread*} "$RESOURCES/python/include" "$RESOURCES/python/share"
  find "$RESOURCES/python/bin" -mindepth 1 ! -name 'python3' ! -name 'python3.12' -exec rm -rf {} +
  # Bytecode records each source path: store bundle-relative ones, and skip the timestamp
  # check, which copying the app would invalidate.
  find "$RESOURCES/python" -name __pycache__ -type d -prune -exec rm -rf {} +
  "$PYTHON" -m compileall -q -j 0 --invalidation-mode unchecked-hash -x '/lib2to3/tests/' \
    -s "$RESOURCES" -p "Audio Sorter.app/Contents/Resources" "$STDLIB"
  mkdir -p "$RESOURCES/beat_this"
  cp "$MODEL" "$RESOURCES/beat_this/final0.ckpt"
else
  python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/python" -m pip install --disable-pip-version-check -r "$ROOT/requirements.txt"
  if [ $DETECT = 1 ]; then
    "$ROOT/.venv/bin/python" -m pip install --disable-pip-version-check -r "$ROOT/requirements-detect.txt"
  fi
  if [ ! -f "$ROOT/config.json" ]; then
    cp "$ROOT/config.example.json" "$ROOT/config.json"
  fi
  # The app offers detection only when the model is present (any later build keeps it).
  if [ -f "$MODEL" ] && "$ROOT/.venv/bin/python" -c "import beat_this.inference" 2>/dev/null; then
    printf '%s' "$MODEL" > "$RESOURCES/detect-model"
  else
    rm -f "$RESOURCES/detect-model"
  fi
  printf '%s' "$ROOT/.venv/bin/python" > "$RESOURCES/python-path"
  printf '%s' "$ROOT/config.json" > "$RESOURCES/config-path"
fi

ICONSET="$ROOT/.build/AudioSorter.iconset"
mkdir -p "$ICONSET"
for SIZE in 16 32 128 256 512; do
  sips -z "$SIZE" "$SIZE" "$SOURCES/icon.png" --out "$ICONSET/icon_${SIZE}x${SIZE}.png" > /dev/null
  DOUBLE=$((SIZE * 2))
  sips -z "$DOUBLE" "$DOUBLE" "$SOURCES/icon.png" --out "$ICONSET/icon_${SIZE}x${SIZE}@2x.png" > /dev/null
done
iconutil -c icns "$ICONSET" -o "$RESOURCES/AudioSorter.icns"
cp "$SOURCES/comments.py" "$SOURCES/edit.py" "$SOURCES/rekordbox.py" "$SOURCES/detect.py" "$ROOT/config.example.json" "$RESOURCES/"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>AudioSorter</string>
<key>CFBundleIdentifier</key><string>com.bottonimusic.audiosorter</string>
<key>CFBundleName</key><string>Audio Sorter</string>
<key>CFBundleIconFile</key><string>AudioSorter.icns</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>$VERSION</string>
<key>CFBundleVersion</key><string>$BUILD_NUMBER</string>
<key>LSMinimumSystemVersion</key><string>$MINIMUM_MACOS</string>
<key>NSHumanReadableCopyright</key><string>© 2026 Bottoni Music LLC. Licensed under the GNU GPL v3 or later.</string>
<key>NSHighResolutionCapable</key><true/>
<key>NSPrincipalClass</key><string>NSApplication</string>
</dict></plist>
PLIST
swiftc -O -target "arm64-apple-macos$MINIMUM_MACOS" "$SOURCES/main.swift" -o "$APP/Contents/MacOS/AudioSorter" -framework Cocoa -framework AVFoundation
# Inside out: every library and the interpreter first, then the app that seals them.
if [ $RELEASE = 1 ]; then
  find "$RESOURCES/python" -type f \( -name '*.so' -o -name '*.dylib' -o -name 'python3.12' \) -print0 \
    | xargs -0 -n 64 codesign "${SIGN_FLAGS[@]}"
fi
codesign "${SIGN_FLAGS[@]}" "$APP"
echo "Built $APP"
