#!/usr/bin/env bash
# House Ava's music share (optional, any time after the install). Run it ONCE, with sudo:
#
#     sudo MUSIC_SHARE=//<nas>/<share>/Music HOUSEAVA_USER=$USER bash deploy/setup-music-share.sh
#
#   1. installs python3-venv and cifs-utils
#   2. asks for the NAS's SMB username and password (typed, never shown, never logged) and saves them to
#      /etc/houseava/music.cred (root only, mode 600)
#   3. mounts $MUSIC_SHARE READ-ONLY at /mnt/music through /etc/fstab — mounted on first use
#      (automount), skipped quietly when the NAS is down (nofail), owned by HOUSEAVA_USER so House Ava can read it
#   4. mounts it now and lists a few folders, so you can see it worked
#   5. loginctl enable-linger $HOUSEAVA_USER — House Ava's user service runs from boot, with nobody logged in
#
# Safe to run again: it asks before replacing the saved login, replaces only its own /etc/fstab line (backing the old
# fstab up first), and leaves everything else alone. Nothing on the NAS is ever written (ro).
set -euo pipefail

SHARE="${MUSIC_SHARE:?set MUSIC_SHARE=//<nas>/<share>/Music}"   # your NAS music share (SMB)
MNT="/mnt/music"
CRED_DIR="/etc/houseava"
CRED="$CRED_DIR/music.cred"
SVC_USER="${HOUSEAVA_USER:-${SUDO_USER:-}}"
[ -n "$SVC_USER" ] || { echo "Set HOUSEAVA_USER=<the user House Ava runs as>."; exit 1; }
TAG="# House Ava music share (deploy/setup-music-share.sh) - the line below"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run me with sudo:  sudo bash $0"
    exit 1
fi
if ! id "$SVC_USER" >/dev/null 2>&1; then
    echo "There's no user '$SVC_USER' on this box (set HOUSEAVA_USER=<name> to use another)."
    exit 1
fi
SVC_UID="$(id -u "$SVC_USER")"
SVC_GID="$(id -g "$SVC_USER")"

echo "== 1. packages: python3-venv cifs-utils"
apt-get update -qq
apt-get install -y python3-venv cifs-utils

echo "== 2. the NAS login (for $SHARE)"
mkdir -p "$CRED_DIR"
chmod 700 "$CRED_DIR"
replace="y"
if [ -s "$CRED" ]; then
    read -r -p "A NAS login is already saved in $CRED. Replace it? [y/N] " replace
fi
if [[ "$replace" =~ ^[Yy] ]]; then
    IFS= read -r -p "SMB username on the NAS (one that can read the music share): " smbuser    # (IFS=: spaces kept)
    IFS= read -r -s -p "SMB password (not shown): " smbpass
    echo
    if [ -z "$smbuser" ] || [ -z "$smbpass" ]; then
        echo "Both the username and the password are needed. Nothing was changed."
        exit 1
    fi
    old_umask="$(umask)"
    umask 077
    tmp="$(mktemp "$CRED_DIR/.music.cred.XXXXXX")"
    printf 'username=%s\npassword=%s\n' "$smbuser" "$smbpass" > "$tmp"    # (printf is a builtin: not in ps)
    umask "$old_umask"
    unset smbpass
    chown root:root "$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$CRED"
    echo "Saved to $CRED (root only)."
else
    echo "Keeping the saved login."
fi

echo "== 3. /etc/fstab: $SHARE -> $MNT (read-only)"
LINE="${SHARE// /\\040} $MNT cifs ro,credentials=$CRED,uid=$SVC_UID,gid=$SVC_GID,iocharset=utf8,vers=3.0,_netdev,nofail,x-systemd.automount 0 0"
mkdir -p "$MNT"
new="$(mktemp)"
# everything but our old line (and its tag) and any other entry for $MNT, then ours at the end
awk -v m="$MNT" -v tag="$TAG" '$0 == tag { next } ($1 !~ /^#/ && $2 == m) { next } { print }' /etc/fstab > "$new"
printf '%s\n%s\n' "$TAG" "$LINE" >> "$new"
if cmp -s "$new" /etc/fstab; then
    echo "fstab already has it."
else
    backup="/etc/fstab.before-houseava.$(date +%Y%m%d-%H%M%S)"
    cp -a /etc/fstab "$backup"
    cat "$new" > /etc/fstab                     # (keeps fstab's own owner and mode)
    echo "fstab updated (the old one is $backup)."
fi
rm -f "$new"

echo "== 4. mounting it"
systemctl daemon-reload
MOUNT_UNIT="$(systemd-escape -p --suffix=mount "$MNT")"
AUTO_UNIT="$(systemd-escape -p --suffix=automount "$MNT")"
systemctl stop "$MOUNT_UNIT" 2>/dev/null || true        # (so new options / a new login take effect)
systemctl restart "$AUTO_UNIT"
if listing="$(timeout 60 ls -1 "$MNT" 2>&1)"; then
    count="$(printf '%s\n' "$listing" | grep -c . || true)"
    echo "$MNT is mounted: $count folders. The first few:"
    head -n 8 <<< "$listing" | sed 's/^/    /'
else
    echo "Couldn't read $MNT:"
    echo "    $listing"
    echo "Check the username / password (run me again to replace them) and that $SHARE exists on the NAS."
    exit 1
fi

echo "== 5. House Ava's service runs from boot (linger for $SVC_USER)"
loginctl enable-linger "$SVC_USER"

echo
echo "Done. Now turn music on in ~/.houseava/houseava_config.json:"
echo '    "music": {"enabled": true, "root": "/mnt/music"}'
echo "and restart House Ava (as $SVC_USER, no sudo):  systemctl --user restart houseava"
