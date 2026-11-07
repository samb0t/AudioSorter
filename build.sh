#!/bin/zsh
set -eu
ROOT="${0:A:h}"
APP="$ROOT/Audio Sorter.app"
python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python" -m pip install --disable-pip-version-check -r "$ROOT/requirements.txt"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
if [ ! -f "$ROOT/config.json" ]; then
  cp "$ROOT/config.example.json" "$ROOT/config.json"
fi
cp "$ROOT/comments.py" "$APP/Contents/Resources/comments.py"
printf '%s' "$ROOT/.venv/bin/python" > "$APP/Contents/Resources/python-path"
printf '%s' "$ROOT/config.json" > "$APP/Contents/Resources/config-path"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>AudioSorter</string>
<key>CFBundleIdentifier</key><string>local.audio-sorter</string>
<key>CFBundleName</key><string>Audio Sorter</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>NSHighResolutionCapable</key><true/>
<key>NSPrincipalClass</key><string>NSApplication</string>
</dict></plist>
PLIST
swiftc "$ROOT/main.swift" -o "$APP/Contents/MacOS/AudioSorter" -framework Cocoa -framework AVFoundation
codesign --force --sign - "$APP"
echo "Built $APP"
