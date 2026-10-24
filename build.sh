#!/bin/bash
# 构建菜单栏 App。改动 tokenlens.py 的路径或移动仓库后，重跑一次即可。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$ROOT/TokenLensBar.app"
BIN="$APP/Contents/MacOS/TokenLensBar"
# 运行时可以装在别处（install.sh 会把它放到 ~/.tokenlens），
# 这样仓库和这块外置盘之后可以随便挪走。
RUNTIME="${TOKENLENS_RUNTIME_DIR:-$ROOT}"

mkdir -p "$APP/Contents/MacOS"
mkdir -p "$ROOT/menu"
# swiftc 的 module cache 不能放在这个外置盘上（会 SIGTRAP），放 /tmp
MODULE_CACHE="/tmp/tokenlens-modcache"
mkdir -p "$MODULE_CACHE"

# 把绝对路径编译进二进制，避免运行时还要找配置文件
cat > "$ROOT/menu/GeneratedConfig.swift" <<EOF
// 由 build.sh 生成，不要手改
import Foundation
let kScriptPath = "$RUNTIME/tokenlens.py"
let kDbPath = "$HOME/.tokenlens/usage.db"
let kPricesPath = "$HOME/.tokenlens/prices.json"
EOF

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>TokenLensBar</string>
    <key>CFBundleDisplayName</key>
    <string>TokenLens</string>
    <key>CFBundleIdentifier</key>
    <string>com.tokenlens.bar</string>
    <key>CFBundleExecutable</key>
    <string>TokenLensBar</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>
    <string>14.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

mkdir -p "$APP/Contents/Resources"
if [ -f "$ROOT/assets/AppIcon.icns" ]; then
    cp "$ROOT/assets/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
fi

swiftc -O -swift-version 5 \
    -module-cache-path "$MODULE_CACHE" \
    -o "$BIN" \
    "$ROOT/menu/TokenLensBar.swift" \
    "$ROOT/menu/GeneratedConfig.swift" \
    -framework Cocoa -lsqlite3

codesign --force --sign - "$APP" >/dev/null 2>&1 || true

echo "构建完成：$APP"
echo "运行时路径：$RUNTIME/tokenlens.py"
