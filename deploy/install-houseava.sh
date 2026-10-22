#!/usr/bin/env bash
# House Ava, part 2 of 2 — the code and its service. Run as the user House Ava runs as, NO sudo, from a copy of the
# house-ava repo:
#
#     bash deploy/install-houseava.sh                      # install / update
#     bash deploy/install-houseava.sh ~/music_index.json   # ... and seed the music index from a desktop Ava one
#
#   1. copies the code to ~/houseava (houseava/, vendor/, faces/, tests/, deploy/, the requirements files)
#   2. makes ~/houseava/.venv (python3 -m venv) and installs requirements.txt into it
#   3. optional: copies a desktop Ava music_index.json to ~/.houseava/desktop_music_index.json and sets music.seed_file,
#      so the library is searchable at once (its music.desktop_root paths are moved onto /mnt/music the first time it runs)
#   4. installs ~/.config/systemd/user/houseava.service (python -m houseava.satellite run) and enables it; starts it
#      when at least one Echo is configured
#
# Run it again after copying a newer project over: it updates in place. Her settings, memory and music index live in
# ~/.houseava and are never touched here (except music.seed_file when you pass a seed).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$HOME/houseava"
DATA="$HOME/.houseava"
UNIT_DIR="$HOME/.config/systemd/user"
SEED="${1:-}"

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as the user House Ava will run as, not root (no sudo)."
    exit 1
fi
if [ ! -f "$SRC/houseava/satellite.py" ] || [ ! -d "$SRC/vendor/ava/tools/musiclib" ]; then
    echo "Run me from inside a copy of the house-ava repo (I look for houseava/ and vendor/ next to deploy/)."
    exit 1
fi
if [ -n "$SEED" ] && [ ! -f "$SEED" ]; then
    echo "No such file: $SEED"
    exit 1
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
    echo "House Ava needs Python 3.11 or newer (aioesphomeapi does)."
    exit 1
}
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"     # (systemctl --user over su / a bare ssh)
if [ ! -d "$XDG_RUNTIME_DIR" ]; then
    echo "There's no user session for $(id -un) yet (common after su). Run this once, then run me again:"
    echo "    sudo loginctl enable-linger $(id -un)"
    exit 1
fi

echo "== 1. code -> $DEST"
mkdir -p "$DEST" "$DATA"
if [ "$SRC" != "$DEST" ]; then
    rm -rf "$DEST/houseava" "$DEST/vendor" "$DEST/faces" "$DEST/tests" "$DEST/deploy"
    cp -r "$SRC/houseava" "$SRC/vendor" "$SRC/faces" "$SRC/tests" "$SRC/deploy" "$DEST/"
    cp "$SRC/requirements.txt" "$SRC/requirements-extra.txt" "$DEST/"
    find "$DEST" -name __pycache__ -type d -prune -exec rm -rf {} +
fi

echo "== 2. venv + requirements"
if [ ! -x "$DEST/.venv/bin/python" ]; then
    python3 -m venv "$DEST/.venv"
fi
"$DEST/.venv/bin/python" -m pip install --upgrade pip
"$DEST/.venv/bin/python" -m pip install -r "$DEST/requirements.txt"

cd "$DEST"
if [ -n "$SEED" ]; then
    echo "== 3. music index seed"
    cp "$SEED" "$DATA/desktop_music_index.json"
    HOUSEAVA_DIR="$DATA" "$DEST/.venv/bin/python" - "$DATA/desktop_music_index.json" <<'EOF'
import sys
from houseava.config import Config
cfg = Config()
cfg.set("music.seed_file", sys.argv[1])
cfg.save()
print("music.seed_file =", sys.argv[1], "in", cfg.path)
EOF
fi

echo "== 4. the service (systemd --user)"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/houseava.service" <<'EOF'
[Unit]
Description=House Ava - the Echo satellite hub (voice, lights, music)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/houseava
Environment=HOUSEAVA_DIR=%h/.houseava
Environment=PYTHONUNBUFFERED=1
# GPU speech-to-text needs LD_LIBRARY_PATH set: put it in a drop-in (systemctl --user edit houseava), since this
# file gets rewritten on every install. See docs/SETUP.md.
ExecStart=%h/houseava/.venv/bin/python -m houseava.satellite run
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable houseava.service
sats="$(HOUSEAVA_DIR="$DATA" "$DEST/.venv/bin/python" -m houseava.satellite list)"
if [ -n "$sats" ]; then
    printf '%s\n' "$sats"
    systemctl --user restart houseava.service
    echo "Started. Watch it:  journalctl --user -u houseava -f"
else
    echo "No Echo configured yet, so it isn't started. Add each one, then start it:"
    echo "    cd ~/houseava && HOUSEAVA_DIR=~/.houseava .venv/bin/python -m houseava.satellite add \\"
    echo "        --name \"Kitchen Echo\" --ip kitchen-echo.local --room kitchen --psk '<key from the TECHO5 installer>'"
    echo "    systemctl --user restart houseava"
fi
