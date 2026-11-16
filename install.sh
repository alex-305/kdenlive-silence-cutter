#!/usr/bin/env bash
set -euo pipefail

APP_ID="kdenlive-silence-cutter"
APP_NAME="Kdenlive Silence Cutter"
MARKER="kdenlive-silence-cutter-managed-install-v1"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

DATA_HOME="${XDG_DATA_HOME:-${HOME:?HOME is not set}/.local/share}"
if [[ "$DATA_HOME" != /* ]]; then
    echo "XDG_DATA_HOME must be an absolute path: $DATA_HOME" >&2
    exit 1
fi

APP_DIR="$DATA_HOME/$APP_ID"
SOURCE_DIR="$APP_DIR/source"
VENV_DIR="$APP_DIR/venv"
APPLICATIONS_DIR="$DATA_HOME/applications"
DESKTOP_FILE="$APPLICATIONS_DIR/$APP_ID.desktop"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3.10 or newer is required. Install Python 3 and retry." >&2
    exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    echo "Python 3.10 or newer is required." >&2
    exit 1
fi
if ! python3 -c 'import venv' >/dev/null 2>&1; then
    echo "The Python venv module is missing. Install your distribution's python-venv package." >&2
    exit 1
fi

missing=()
for tool in ffmpeg ffprobe; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        missing+=("$tool")
    fi
done
if ((${#missing[@]})); then
    echo "Missing required media tools: ${missing[*]}" >&2
    echo "Install FFmpeg with your distribution's package manager, then rerun this script." >&2
    if command -v apt-get >/dev/null 2>&1; then
        echo "  Debian/Ubuntu: sudo apt install ffmpeg" >&2
    elif command -v dnf >/dev/null 2>&1; then
        echo "  Fedora: sudo dnf install ffmpeg" >&2
    elif command -v pacman >/dev/null 2>&1; then
        echo "  Arch: sudo pacman -S ffmpeg" >&2
    elif command -v zypper >/dev/null 2>&1; then
        echo "  openSUSE: sudo zypper install ffmpeg" >&2
    fi
    exit 1
fi

if [[ -e "$APP_DIR" || -L "$APP_DIR" ]]; then
    if [[ -L "$APP_DIR" || -L "$APP_DIR/.install-marker" ||
          ! -f "$APP_DIR/.install-marker" ]] || \
       [[ "$(<"$APP_DIR/.install-marker")" != "$MARKER" ]]; then
        echo "Refusing to use an existing, unmanaged install directory: $APP_DIR" >&2
        echo "Move it aside or remove it deliberately, then retry." >&2
        exit 1
    fi
fi
if [[ -L "$SOURCE_DIR" || -L "$VENV_DIR" ]]; then
    echo "Refusing to install through a symlink in the managed app directory." >&2
    exit 1
fi
if [[ -L "$DESKTOP_FILE" ]] || \
   { [[ -e "$DESKTOP_FILE" ]] && ! grep -q '^X-KdenliveSilenceCutter-Managed=true$' "$DESKTOP_FILE"; }; then
    echo "Refusing to overwrite an unmanaged desktop entry: $DESKTOP_FILE" >&2
    exit 1
fi

mkdir -p "$APP_DIR" "$APPLICATIONS_DIR"
printf '%s\n' "$MARKER" > "$APP_DIR/.install-marker"
install -m 0755 "$SCRIPT_DIR/uninstall.sh" "$APP_DIR/uninstall.sh"

if [[ "$SCRIPT_DIR" != "$SOURCE_DIR" ]]; then
    INSTALL_SOURCE_DIR="$SOURCE_DIR" python3 - "$SCRIPT_DIR" <<'PY'
import os
import shutil
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(os.environ["INSTALL_SOURCE_DIR"])
ignore = shutil.ignore_patterns(
    ".git", ".venv", "venv", "__pycache__", "*.pyc", "*.egg-info",
    ".pytest_cache", ".mypy_cache", "build", "dist", "tests",
)
shutil.copytree(source, destination, dirs_exist_ok=True, ignore=ignore)
PY
fi

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --upgrade "$SOURCE_DIR[gui]"

INSTALL_DESKTOP_PATH="$DESKTOP_FILE" \
INSTALL_EXEC_PATH="$VENV_DIR/bin/kdenlive-silence-cutter-gui" \
INSTALL_NAME="$APP_NAME" python3 <<'PY'
import os
from pathlib import Path

def quote_exec(value: str) -> str:
    # Desktop Entry Exec quoting (not shell quoting).
    escaped = "".join("\\" + char if char in '\\"`$' else "%%" if char == "%" else char
                      for char in value)
    return f'"{escaped}"'

desktop_path = Path(os.environ["INSTALL_DESKTOP_PATH"])
executable_path = os.environ["INSTALL_EXEC_PATH"]
executable = quote_exec(executable_path)
try_exec = executable_path.replace("\\", "\\\\")
name = os.environ["INSTALL_NAME"]
contents = f"""[Desktop Entry]
Version=1.0
Type=Application
Name={name}
GenericName=Silence Cutter for Kdenlive
Comment=Preview audio waveforms and remove silence from Kdenlive projects
Exec={executable}
TryExec={try_exec}
Icon=applications-multimedia
Terminal=false
StartupNotify=true
Categories=AudioVideo;AudioVideoEditing;
Keywords=kdenlive;audio;waveform;silence;video;
X-KdenliveSilenceCutter-Managed=true
"""
desktop_path.write_text(contents, encoding="utf-8")
desktop_path.chmod(0o644)
PY

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPLICATIONS_DIR" >/dev/null 2>&1 || true
fi

echo "Installed $APP_NAME for the current user."
echo "Launcher: $DESKTOP_FILE"
echo "Start it from the applications menu or run: $VENV_DIR/bin/kdenlive-silence-cutter-gui"
echo "To remove it, run: $APP_DIR/uninstall.sh"
