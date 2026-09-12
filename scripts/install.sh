#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP_ROOT="${HOME}/.local/lib/localcode"
BIN_DIR="${HOME}/.local/bin"
DATA_HOME="${XDG_DATA_HOME:-${HOME}/.local/share}"
APPLICATIONS_DIR="${DATA_HOME}/applications"
ICON_DIR="${DATA_HOME}/icons/hicolor/scalable/apps"
METAINFO_DIR="${DATA_HOME}/metainfo"

mkdir -p "$APP_ROOT" "$BIN_DIR" "$APPLICATIONS_DIR" "$ICON_DIR" "$METAINFO_DIR"
rm -rf "$APP_ROOT/localcode" "$APP_ROOT/vendor/mempalace"
cp -a "$ROOT/localcode" "$APP_ROOT/"
install -m 755 "$ROOT/localcode.py" "$APP_ROOT/localcode.py"
install -m 644 "$ROOT/data/io.localcode.LocalCode.desktop" "$APPLICATIONS_DIR/io.localcode.LocalCode.desktop"
install -m 644 "$ROOT/data/io.localcode.LocalCode.svg" "$ICON_DIR/io.localcode.LocalCode.svg"
install -m 644 "$ROOT/data/io.localcode.LocalCode.metainfo.xml" "$METAINFO_DIR/io.localcode.LocalCode.metainfo.xml"
ln -sfn "$APP_ROOT/localcode.py" "$BIN_DIR/localcode"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPLICATIONS_DIR"
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "${DATA_HOME}/icons/hicolor" >/dev/null 2>&1 || true
fi

printf '%s\n' "Installed LocalCode. Launch it from GNOME or run: $BIN_DIR/localcode"
