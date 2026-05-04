#!/usr/bin/env bash
set -euo pipefail

APP_ID="linux-uninstaller"
APP_NAME="Linux Uninstaller"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
DESKTOP_FILE="$DATA_HOME/applications/$APP_ID.desktop"
ICON_FILE="$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg"
USER_DESKTOP_DIR="${XDG_DESKTOP_DIR:-$HOME/Desktop}"
DESKTOP_SHORTCUT="$USER_DESKTOP_DIR/$APP_NAME.desktop"

rm -f "$DESKTOP_FILE" "$ICON_FILE" "$DESKTOP_SHORTCUT"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$DATA_HOME/applications" >/dev/null 2>&1 || true
fi

printf 'Removed desktop app files for %s.\n' "$APP_ID"