"""Playlists as .m3u8 files (UTF-8 M3U: Winamp, VLC, phones and the satellites all read them).

    <data>\\Now Playing.m3u8           the list she's playing now (rewritten for every new list)
    <data>\\Playlists\\Road Trip.m3u8   saved playlists ("save this as road trip"), one file each
    <data>\\Playlists\\Deleted\\        a deleted playlist goes here (it was only ever a list of paths)
    <data>\\music_session.json         what she played last (so "this song" / "save this" know what you mean)

A player with a key (one per room, player.py) has its own "Now Playing - kitchen.m3u8" / "music_session-kitchen.json";
saved playlists are shared. <data> is player.data_dir() (Ava's data folder unless a service set its own).

Every entry is a PATH to a song in your music folders; nothing here ever touches the songs themselves.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from difflib import SequenceMatcher

from ...log import get
from . import player, vocab


_RXC: dict = {}


def _rx(p: str):
    """A compiled pattern from this module's own cache (never Python's shared 512-entry re cache, which the
    router's phrasebook already fills)."""
    r = _RXC.get(p)
    if r is None:
        r = _RXC[p] = re.compile(p)
    return r


log = get()

NOW_FILE = "Now Playing.m3u8"
SESSION_FILE = "music_session.json"
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def data_dir():
    return player.data_dir()


def _keyed(name: str, sep: str) -> str:
    """'Now Playing.m3u8' -> 'Now Playing - kitchen.m3u8' for a player with a key (its own files); unchanged for the
    desktop's player (no key). A key never becomes a path: separators and the like turn into '_' — and a key that had
    to be cleaned or cut gets a short hash of itself, so 'echo/kitchen' and 'echo:kitchen' never share a file."""
    raw = str(player.get().key or "")
    if not raw:
        return name
    key = _BAD.sub("_", raw).strip(" .")[:48] or "_"
    if key != raw:
        key += "-" + hashlib.sha1(raw.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    stem, ext = os.path.splitext(name)
    return f"{stem}{sep}{key}{ext}"


def folder():
    p = data_dir() / "Playlists"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


# ---------------------------------------------------------------- m3u8
def write_m3u8(path, tracks) -> None:
    """tracks: [{"path", "artist"?, "title"?, "duration"?}] or plain paths."""
    lines = ["#EXTM3U"]
    for t in tracks:
        if isinstance(t, str):
            t = {"path": t}
        p = t.get("path")
        if not p:
            continue
        dur = int(round(float(t.get("duration") or 0))) or -1
        who = (t.get("artist") or t.get("main_artist") or "").strip()
        what = (t.get("title") or os.path.splitext(os.path.basename(p))[0]).strip()
        lines.append(f"#EXTINF:{dur},{who + ' - ' if who else ''}{what}")
        lines.append(p)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, str(path))


def read_m3u8(path) -> list[str]:
    out = []
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            base = os.path.dirname(str(path))
            for ln in fh:
                s = ln.strip()
                if not s or s.startswith("#"):
                    continue
                # (a rooted path — /mnt/music/... on a Linux service — is never joined onto the list's folder, even
                # on a Python where Windows calls it relative)
                if not os.path.isabs(s) and s[:1] not in ("/", "\\") and not _rx(r"(?i)^[a-z]+://").match(s):
                    s = os.path.normpath(os.path.join(base, s))
                out.append(s)
    except OSError:
        return []
    return out


# ---------------------------------------------------------------- saved playlists
def clean_name(name: str) -> str:
    """'my road trip playlist' -> 'road trip' (what the list is CALLED)."""
    n = _rx(r"\s+").sub(" ", (name or "").lower()).strip(" .,!?'\"")
    for _ in range(3):
        n = _rx(r"^(?:my|the|our|a|an|new|this|that|called|named|titled)\s+").sub("", n)
        n = _rx(r"\s+(?:playlist|play list|playlists|list|mix|mixtape)$").sub("", n)
        n = _rx(r"^(?:playlist|play list)\s+(?:called|named|titled)?\s*").sub("", n)
    return n.strip(" .,!?'\"")


def display(name: str) -> str:
    n = clean_name(name)
    return " ".join(w if (w.isupper() and len(w) > 1) or any(c.isdigit() for c in w) else w.capitalize()
                    for w in n.split())


def _file_for(name: str):
    return folder() / (_BAD.sub("", display(name)).strip() + ".m3u8")


def saved() -> dict[str, str]:
    """{shown name: file path} of every saved playlist (never creates the folder: this runs while matching)."""
    out = {}
    try:
        p = data_dir() / "Playlists"
        if not p.is_dir():
            return out
        for f in sorted(p.glob("*.m3u8"), key=lambda q: q.name.lower()):
            out[f.stem] = str(f)
    except Exception:
        pass
    return out


def exists(name: str) -> bool:
    try:
        return find(name) is not None
    except Exception:
        return False


def find(name: str, cutoff: float = 0.8) -> str | None:
    """The saved playlist this name means ('rode trip' -> 'Road Trip'), else None."""
    want = vocab.akey(clean_name(name))
    if not want:
        return None
    best, score = None, 0.0
    for shown in saved():
        k = vocab.akey(shown)
        if k == want:
            return shown
        s = max(SequenceMatcher(None, want, k).ratio(), 0.97 if vocab.pkey(want) == vocab.pkey(k) else 0.0)
        if s > score:
            best, score = shown, s
    return best if score >= cutoff else None


def load(name: str) -> list[str]:
    shown = find(name)
    if not shown:
        return []
    return read_m3u8(saved()[shown])


def save(name: str, tracks) -> str:
    """Write (replace) a saved playlist; -> its shown name. (Only the SAME name is replaced — 'road trips' is a new
    list, never written over 'Road Trip'.)"""
    shown = find(name, cutoff=0.95) or display(name)
    path = folder() / (_BAD.sub("", shown).strip() + ".m3u8")
    write_m3u8(path, tracks)
    return shown


def rename(old: str, new: str) -> tuple[str, str] | None:
    shown = find(old)
    if not shown:
        return None
    src = saved()[shown]
    dst = _file_for(new)
    if os.path.exists(dst) and os.path.normcase(str(dst)) != os.path.normcase(src):
        raise FileExistsError(display(new))
    os.replace(src, dst)
    return shown, dst.stem


def remove(name: str) -> str | None:
    """Delete a saved playlist (the LIST — moved to Playlists\\Deleted, never a song). -> its name."""
    shown = find(name, cutoff=0.9)
    if not shown:
        return None
    src = saved()[shown]
    bin_ = folder() / "Deleted"
    bin_.mkdir(parents=True, exist_ok=True)
    dst = bin_ / os.path.basename(src)
    if dst.exists():
        dst = bin_ / f"{dst.stem} ({time.strftime('%Y-%m-%d %H%M%S')}).m3u8"
    os.replace(src, dst)
    return shown


# ---------------------------------------------------------------- the session: what's playing
def session_file():
    return data_dir() / _keyed(SESSION_FILE, "-")


def session() -> dict:
    try:
        return json.loads(session_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def set_session(**kw) -> None:
    s = {"ts": time.time(), **kw}
    try:
        p = session_file()
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(s), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as exc:
        log.debug("music session not saved: %s", exc)


def now_file():
    return data_dir() / _keyed(NOW_FILE, " - ")


# ---------------------------------------------------------------- opening a list in the player
def launch(path: str, cfg=None) -> dict:
    """Open an .m3u8 in the player in use (player.py: Winamp, else the default player, on the desktop). A test seam."""
    return player.get().open_list(str(path), cfg)


def play(tracks, label: str, cfg=None, **sess) -> dict:
    """Write Now Playing.m3u8 with these tracks (in this order), open it, remember the session."""
    tracks = [t if isinstance(t, dict) else {"path": t} for t in tracks]
    if not tracks:
        return {"ok": False, "say": "There's nothing to play."}
    path = now_file()
    try:
        write_m3u8(path, tracks)
    except Exception as exc:
        return {"ok": False, "say": f"I couldn't write the playlist: {exc}"}
    r = launch(str(path), cfg)
    if not r.get("ok"):
        return r
    set_session(kind="list", label=label, paths=[t["path"] for t in tracks][:5000], **sess)
    return {"ok": True, "player": r.get("player", player.get().name), "playlist_file": str(path), "count": len(tracks)}
