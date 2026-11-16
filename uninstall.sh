#!/usr/bin/env bash
set -euo pipefail

APP_ID="kdenlive-silence-cutter"
MARKER="kdenlive-silence-cutter-managed-install-v1"
DATA_HOME="${XDG_DATA_HOME:-${HOME:?HOME is not set}/.local/share}"
if [[ "$DATA_HOME" != /* ]]; then
    echo "XDG_DATA_HOME must be an absolute path: $DATA_HOME" >&2
    exit 1
fi

APP_DIR="$DATA_HOME/$APP_ID"
DESKTOP_FILE="$DATA_HOME/applications/$APP_ID.desktop"

if (($# > 1)) || { (($# == 1)) && [[ "$1" != "--yes" && "$1" != "-y" ]]; }; then
    echo "Usage: $0 [--yes]" >&2
    exit 2
fi

if [[ ! -d "$APP_DIR" ]]; then
    echo "No installation found at $APP_DIR"
    exit 0
fi
if [[ -L "$APP_DIR" || -L "$APP_DIR/.install-marker" || ! -f "$APP_DIR/.install-marker" ]] || \
   [[ "$(<"$APP_DIR/.install-marker")" != "$MARKER" ]]; then
    echo "Refusing to remove an unmanaged directory: $APP_DIR" >&2
    exit 1
fi

if [[ "${1:-}" != "--yes" && "${1:-}" != "-y" ]]; then
    read -r -p "Remove the current user's Kdenlive Silence Cutter installation? [y/N] " answer
    case "$answer" in
        y|Y|yes|YES) ;;
        *) echo "Cancelled."; exit 0 ;;
    esac
fi

if [[ -L "$DESKTOP_FILE" ]]; then
    echo "Refusing to remove a symlink desktop entry: $DESKTOP_FILE" >&2
    exit 1
fi
if [[ -f "$DESKTOP_FILE" ]] && grep -q '^X-KdenliveSilenceCutter-Managed=true$' "$DESKTOP_FILE"; then
    rm -- "$DESKTOP_FILE"
fi
rm -rf -- "$APP_DIR"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DATA_HOME/applications" >/dev/null 2>&1 || true
fi

echo "Uninstalled Kdenlive Silence Cutter. Waveform cache and project files were left untouched."
