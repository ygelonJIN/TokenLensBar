#!/bin/bash
# 一次装好：运行时 + 菜单栏 App + 每 5 分钟采集 + 登录自启
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
RUNTIME="$HOME/.tokenlens"
AGENTS="$HOME/Library/LaunchAgents"

# /Applications 一般可写；不行就退到用户目录
if [ -w /Applications ]; then
    APP_TARGET="/Applications/TokenLensBar.app"
else
    APP_TARGET="$HOME/Applications/TokenLensBar.app"
    mkdir -p "$HOME/Applications"
fi

echo "1/6  运行时 → $RUNTIME"
mkdir -p "$RUNTIME/web"
cp "$ROOT/tokenlens.py" "$RUNTIME/tokenlens.py"
cp "$ROOT/web/index.html" "$RUNTIME/web/index.html"
if [ ! -f "$RUNTIME/prices.json" ]; then
    cp "$ROOT/prices.json" "$RUNTIME/prices.json"
    echo "     已放置单价表：$RUNTIME/prices.json"
fi

echo "2/6  构建 App（指向运行时目录，仓库之后可以挪走）"
TOKENLENS_RUNTIME_DIR="$RUNTIME" "$ROOT/build.sh" >/dev/null

echo "3/6  安装 App → $APP_TARGET"
rm -rf "$APP_TARGET"
cp -R "$ROOT/TokenLensBar.app" "$APP_TARGET"

echo "4/6  每 5 分钟采集"
mkdir -p "$AGENTS"
cat > "$AGENTS/com.tokenlens.scan.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.tokenlens.scan</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$RUNTIME/tokenlens.py</string>
        <string>scan</string>
    </array>
    <key>StartInterval</key><integer>300</integer>
    <key>RunAtLoad</key><true/>
    <key>ProcessType</key><string>Background</string>
    <key>LowPriorityIO</key><true/>
    <key>StandardOutPath</key><string>$RUNTIME/scan.log</string>
    <key>StandardErrorPath</key><string>$RUNTIME/scan.log</string>
</dict>
</plist>
PLIST

echo "5/6  登录自启菜单栏 App"
cat > "$AGENTS/com.tokenlens.bar.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.tokenlens.bar</string>
    <key>ProgramArguments</key>
    <array>
        <string>$APP_TARGET/Contents/MacOS/TokenLensBar</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>LimitLoadToSessionType</key><string>Aqua</string>
</dict>
</plist>
PLIST

launchctl unload "$AGENTS/com.tokenlens.scan.plist" 2>/dev/null || true
launchctl unload "$AGENTS/com.tokenlens.bar.plist" 2>/dev/null || true
launchctl load "$AGENTS/com.tokenlens.scan.plist"
launchctl load "$AGENTS/com.tokenlens.bar.plist" 2>/dev/null || true

echo "6/6  首次采集"
python3 "$RUNTIME/tokenlens.py" scan

echo
echo "装好了："
echo "  App      $APP_TARGET"
echo "  数据     $RUNTIME/usage.db"
echo "  单价表   $RUNTIME/prices.json"
echo "  看板     python3 $RUNTIME/tokenlens.py serve"
echo
echo "菜单栏右上角会出现一个 token 用量数字。没出现就手动开一次："
echo "  open \"$APP_TARGET\""
