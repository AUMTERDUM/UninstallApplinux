#!/usr/bin/env bash
set -euo pipefail

APP_ID="linux-uninstaller"
APP_NAME="Linux Uninstaller"
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
DESKTOP_DIR="$DATA_HOME/applications"
ICON_DIR="$DATA_HOME/icons/hicolor/scalable/apps"
USER_DESKTOP_DIR="${XDG_DESKTOP_DIR:-$HOME/Desktop}"
DESKTOP_FILE="$DESKTOP_DIR/$APP_ID.desktop"
DESKTOP_SHORTCUT="$USER_DESKTOP_DIR/$APP_NAME.desktop"
ICON_FILE="$ICON_DIR/$APP_ID.svg"

mkdir -p "$DESKTOP_DIR" "$ICON_DIR" "$USER_DESKTOP_DIR"

chmod +x "$BASE_DIR/run.sh"
cp "$BASE_DIR/assets/$APP_ID.svg" "$ICON_FILE"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=$APP_NAME
Name[th]=ถอนการติดตั้งแอป
Comment=View installed apps and uninstall Linux packages from one screen
Exec=$BASE_DIR/run.sh
Path=$BASE_DIR
Icon=$APP_ID
Terminal=false
Categories=System;Settings;Utility;
Keywords=uninstall;package;apt;snap;flatpak;linux;
StartupNotify=true
SingleMainWindow=true
EOF

cp "$DESKTOP_FILE" "$DESKTOP_SHORTCUT"
chmod +x "$DESKTOP_FILE" "$DESKTOP_SHORTCUT"

if command -v gio >/dev/null 2>&1; then
  gio set "$DESKTOP_SHORTCUT" metadata::trusted true >/dev/null 2>&1 || true
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
fi

printf 'Installed desktop app.\nLauncher: %s\nIcon: %s\n' "$DESKTOP_FILE" "$ICON_FILE"
printf 'Desktop shortcut: %s\n' "$DESKTOP_SHORTCUT"
printf 'Open it from your app menu as "%s".\n' "$APP_NAME"