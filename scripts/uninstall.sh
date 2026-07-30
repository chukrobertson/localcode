#!/bin/sh
set -eu

APP_ROOT="${HOME}/.local/lib/localcode"
BIN_PATH="${HOME}/.local/bin/localcode"
DATA_HOME="${XDG_DATA_HOME:-${HOME}/.local/share}"

rm -rf "$APP_ROOT"
rm -f "$BIN_PATH"
rm -f "$DATA_HOME/applications/io.localcode.LocalCode.desktop"
rm -f "$DATA_HOME/icons/hicolor/scalable/apps/io.localcode.LocalCode.svg"
rm -f "$DATA_HOME/metainfo/io.localcode.LocalCode.metainfo.xml"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$DATA_HOME/applications"
fi

printf '%s\n' "Uninstalled LocalCode. Chats and MemPalace data remain in $DATA_HOME/localcode."
