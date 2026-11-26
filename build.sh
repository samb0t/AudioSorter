#!/bin/zsh
set -eu
ROOT="${0:A:h}"
APP="$ROOT/Audio Sorter.app"
python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python" -m pip install --disable-pip-version-check -r "$ROOT/requirements.txt"
# Grid detection is opt-in: --detect installs beat_this and downloads its final0 model once.
MODEL="$ROOT/.build/beat_this/final0.ckpt"
MODEL_SHA256=8c328b45f59d8dd3dff219253ff6a8d6482be57d0133a29140e2febbf8eb8331
if [ "${1:-}" = "--detect" ]; then
  "$ROOT/.venv/bin/python" -m pip install --disable-pip-version-check -r "$ROOT/requirements-detect.txt"
  if [ ! -f "$MODEL" ]; then
    mkdir -p "${MODEL:h}"
    curl -fL --progress-bar -o "$MODEL.part" "https://cloud.cp.jku.at/public.php/dav/files/7ik4RrBKTS273gp/final0.ckpt"
    if [ "$(shasum -a 256 "$MODEL.part" | cut -d' ' -f1)" != "$MODEL_SHA256" ]; then
      rm -f "$MODEL.part"
      echo "The downloaded beat_this model failed its checksum." >&2
      exit 1
    fi
    mv "$MODEL.part" "$MODEL"
  fi
fi
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
ICONSET="$ROOT/.build/AudioSorter.iconset"
mkdir -p "$ICONSET"
for SIZE in 16 32 128 256 512; do
  sips -z "$SIZE" "$SIZE" "$ROOT/icon-concept.png" --out "$ICONSET/icon_${SIZE}x${SIZE}.png" > /dev/null
  DOUBLE=$((SIZE * 2))
  sips -z "$DOUBLE" "$DOUBLE" "$ROOT/icon-concept.png" --out "$ICONSET/icon_${SIZE}x${SIZE}@2x.png" > /dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AudioSorter.icns"
if [ ! -f "$ROOT/config.json" ]; then
  cp "$ROOT/config.example.json" "$ROOT/config.json"
fi
cp "$ROOT/comments.py" "$ROOT/edit.py" "$ROOT/rekordbox.py" "$ROOT/detect.py" "$APP/Contents/Resources/"
# The app offers detection only when the model is present (any later build keeps it).
if [ -f "$MODEL" ] && "$ROOT/.venv/bin/python" -c "import beat_this.inference" 2>/dev/null; then
  printf '%s' "$MODEL" > "$APP/Contents/Resources/detect-model"
else
  rm -f "$APP/Contents/Resources/detect-model"
fi
printf '%s' "$ROOT/.venv/bin/python" > "$APP/Contents/Resources/python-path"
printf '%s' "$ROOT/config.json" > "$APP/Contents/Resources/config-path"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>AudioSorter</string>
<key>CFBundleIdentifier</key><string>local.audio-sorter</string>
<key>CFBundleName</key><string>Audio Sorter</string>
<key>CFBundleIconFile</key><string>AudioSorter.icns</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>NSHumanReadableCopyright</key><string>© 2026 Bottoni Music LLC. Licensed under the GNU GPL v3 or later.</string>
<key>NSHighResolutionCapable</key><true/>
<key>NSPrincipalClass</key><string>NSApplication</string>
</dict></plist>
PLIST
swiftc -O "$ROOT/main.swift" -o "$APP/Contents/MacOS/AudioSorter" -framework Cocoa -framework AVFoundation
codesign --force --sign - "$APP"
echo "Built $APP"
