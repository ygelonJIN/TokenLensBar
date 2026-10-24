#!/bin/bash
# 一次装好：运行时 + 菜单栏 App + 每 5 分钟采集 + 登录自启
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
RUNTIME="$HOME/.tokenlensbar"
LEGACY_RUNTIME="$HOME/.tokenlens"
AGENTS="$HOME/Library/LaunchAgents"
SCAN_LABEL="com.tokenlensbar.scan"
APP_LABEL="com.tokenlensbar.bar"

# /Applications 一般可写；不行就退到用户目录
if [ -w /Applications ]; then
    APP_TARGET="/Applications/TokenLensBar.app"
else
    APP_TARGET="$HOME/Applications/TokenLensBar.app"
    mkdir -p "$HOME/Applications"
fi

echo "1/7  清理旧版本（tokenlens → TokenLensBar）"
# 旧版 App 可能还在跑，先停掉，否则覆盖不了 bundle、也会继续写旧库
pkill -f "$APP_TARGET/Contents/MacOS/TokenLensBar" 2>/dev/null || true
for label in com.tokenlens.scan com.tokenlens.bar; do
    plist="$AGENTS/$label.plist"
    if [ -e "$plist" ]; then
        launchctl unload "$plist" 2>/dev/null || true
        rm -f "$plist"
        echo "     已移除旧任务：$label"
    fi
done
if [ -d "$LEGACY_RUNTIME" ] && [ ! -e "$RUNTIME" ]; then
    mv "$LEGACY_RUNTIME" "$RUNTIME"
    echo "     已迁移数据目录：$LEGACY_RUNTIME → $RUNTIME"
elif [ -d "$LEGACY_RUNTIME" ]; then
    echo "     注意：$LEGACY_RUNTIME 仍存在（新目录已存在），未自动合并；确认后手动删除"
fi
rm -f "$RUNTIME/tokenlens.py"

echo "2/7  运行时 → $RUNTIME"
mkdir -p "$RUNTIME/web"
cp "$ROOT/tokenlensbar.py" "$RUNTIME/tokenlensbar.py"
cp "$ROOT/web/index.html" "$RUNTIME/web/index.html"
if [ ! -f "$RUNTIME/prices.json" ]; then
    cp "$ROOT/prices.json" "$RUNTIME/prices.json"
    echo "     已放置单价表：$RUNTIME/prices.json"
fi
if [ ! -f "$RUNTIME/balances.json" ]; then
    cp "$ROOT/balances.example.json" "$RUNTIME/balances.json"
    echo "     已启用 DeepSeek 余额模板：$RUNTIME/balances.json"
fi
if [ ! -f "$RUNTIME/secrets.example.json" ]; then
    cp "$ROOT/secrets.example.json" "$RUNTIME/secrets.example.json"
fi
if [ ! -f "$RUNTIME/secrets.json" ]; then
    cp "$ROOT/secrets.example.json" "$RUNTIME/secrets.json"
    chmod 600 "$RUNTIME/secrets.json"
    echo "     已放置余额 Key 模板：$RUNTIME/secrets.json（把占位符换成完整 Key）"
fi

echo "3/7  构建 App（指向运行时目录，仓库之后可以挪走）"
TOKENLENSBAR_RUNTIME_DIR="$RUNTIME" "$ROOT/build.sh" >/dev/null

echo "4/7  安装 App → $APP_TARGET"
rm -rf "$APP_TARGET"
cp -R "$ROOT/TokenLensBar.app" "$APP_TARGET"

echo "5/7  每 5 分钟采集"
mkdir -p "$AGENTS"
cat > "$AGENTS/$SCAN_LABEL.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$SCAN_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$RUNTIME/tokenlensbar.py</string>
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

echo "6/7  登录自启菜单栏 App"
cat > "$AGENTS/$APP_LABEL.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$APP_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$APP_TARGET/Contents/MacOS/TokenLensBar</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>LimitLoadToSessionType</key><string>Aqua</string>
</dict>
</plist>
PLIST

launchctl unload "$AGENTS/$SCAN_LABEL.plist" 2>/dev/null || true
launchctl unload "$AGENTS/$APP_LABEL.plist" 2>/dev/null || true
launchctl load "$AGENTS/$SCAN_LABEL.plist"
launchctl load "$AGENTS/$APP_LABEL.plist" 2>/dev/null || true

echo "7/7  首次采集"
python3 "$RUNTIME/tokenlensbar.py" scan

echo
echo "装好了："
echo "  App      $APP_TARGET"
echo "  数据     $RUNTIME/usage.db"
echo "  单价表   $RUNTIME/prices.json"
echo "  余额     $RUNTIME/balances.json（DeepSeek 已启用）"
echo "  余额 Key $RUNTIME/secrets.json（编辑它，把占位符换成完整 Key；权限 600）"
echo "  看板     python3 $RUNTIME/tokenlensbar.py serve"
echo
echo "菜单栏右上角会出现一个 token 用量数字。没出现就手动开一次："
echo "  open \"$APP_TARGET\""
