#!/bin/bash
# Build "Tri Coach.app" — a native WKWebView shell around the local dashboard.
# Reuses coach/static/master1024.png for the icon. Installs to ~/Applications.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
COACH="$(cd "$HERE/.." && pwd)"
APP="$HOME/Applications/Tri Coach.app"
ICON_SRC="$COACH/static/master1024.png"

echo "→ Compiling Objective-C shell…"
BIN="$HERE/TriCoach"
clang -O2 -fobjc-arc -o "$BIN" "$HERE/TriCoach.m" \
  -framework Cocoa -framework WebKit

echo "→ Assembling bundle at: $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
mv "$BIN" "$APP/Contents/MacOS/TriCoach"

# --- Icon: build .icns from the 1024 master ---
if [[ -f "$ICON_SRC" ]]; then
  echo "→ Building app icon…"
  ICONSET="$HERE/TriCoach.iconset"
  rm -rf "$ICONSET"; mkdir -p "$ICONSET"
  for s in 16 32 64 128 256 512 1024; do
    sips -z $s $s "$ICON_SRC" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  done
  # retina @2x variants
  cp "$ICONSET/icon_32x32.png"     "$ICONSET/icon_16x16@2x.png"
  cp "$ICONSET/icon_64x64.png"     "$ICONSET/icon_32x32@2x.png"
  cp "$ICONSET/icon_256x256.png"   "$ICONSET/icon_128x128@2x.png"
  cp "$ICONSET/icon_512x512.png"   "$ICONSET/icon_256x256@2x.png"
  cp "$ICONSET/icon_1024x1024.png" "$ICONSET/icon_512x512@2x.png"
  rm -f "$ICONSET/icon_64x64.png" "$ICONSET/icon_1024x1024.png"
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
  rm -rf "$ICONSET"
fi

# --- Info.plist ---
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Tri Coach</string>
  <key>CFBundleDisplayName</key><string>Tri Coach</string>
  <key>CFBundleIdentifier</key><string>com.tricoach.desktop</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>TriCoach</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <!-- allow http://127.0.0.1 (App Transport Security) -->
  <key>NSAppTransportSecurity</key><dict>
    <key>NSAllowsLocalNetworking</key><true/>
  </dict>
</dict></plist>
PLIST

echo "→ Ad-hoc code-signing…"
codesign --force --deep -s - "$APP" 2>/dev/null || echo "  (codesign skipped)"

# refresh Finder/Dock icon cache for this bundle
touch "$APP"

echo "✅ Built: $APP"
echo "   Open with:  open \"$APP\""
