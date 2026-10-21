"""Find a song in your music folders and play it in Winamp.

A library can be large (a quarter million tracks, thousands of artist folders, on the network),
so a full scan takes minutes. Two ways in:

  * artist named ("play billie jean by michael jackson", or the words match an artist folder):
    only that artist's folder is searched — seconds, no index needed;
  * title only ("play billie jean"): searched in an index of every track, built once in the
    background, kept on disk, refreshed daily. Until the first index is ready she says so
    and asks for the artist.

Ava only opens a file in a player — she never changes, moves or deletes anything.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from difflib import SequenceMatcher

from ..log import get

log = get()

AUDIO_EXT = (".mp3", ".flac", ".m4a", ".wav", ".ogg", ".wma", ".aac", ".opus", ".aif", ".aiff")
DEFAULT_FOLDERS: list = []
_WINAMP_PATHS = [r"C:\Program Files (x86)\Winamp\winamp.exe", r"C:\Program Files\Winamp\winamp.exe"]
_STOP = {"the", "a", "an", "song", "track", "tune", "please", "me", "to", "for"}
_VERSION_WORDS = {"remix", "mix", "live", "edit", "version", "instrumental", "karaoke", "demo", "acoustic",
                  "extended", "remaster", "remastered", "radio", "dub", "cover", "reprise", "rmx", "vip"}
_INDEX_TTL = 24 * 3600
_ARTIST_TTL = 6 * 3600

_lock = threading.Lock()
_index: dict | None = None            # {"built": ts, "files": [abs paths]}
_building = threading.Event()
_artists: dict[str, tuple[float, list[str]]] = {}


# ---- config / players ----------------------------------------------------------------
def folders(cfg) -> list[str]:
    raw = cfg.get("media.music_folders", None) if hasattr(cfg, "get") else None
    return [str(f) for f in (raw or DEFAULT_FOLDERS) if f]


def winamp_path(cfg=None) -> str:
    if cfg is not None and hasattr(cfg, "get"):
        p = str(cfg.get("media.music_player", "") or "")
        if p and os.path.isfile(p):
            return p
    for p in _WINAMP_PATHS:
        if os.path.isfile(p):
            return p
    return ""


# ---- matching ------------------------------------------------------------------------
def _words(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower().replace("'", "")) if w not in _STOP]


def _track_score(qw: list[str], path: str) -> float:
    """Title words dominate; the artist/album folders count too ('thriller michael jackson')."""
    stem = re.sub(r"^\d+[\s.\-_]+", "", os.path.splitext(os.path.basename(path))[0])   # drop "03 - "
    album = os.path.basename(os.path.dirname(path))
    artist = os.path.basename(os.path.dirname(os.path.dirname(path)))
    # files are "Artist & Guest - Title": only the title counts as the song's name
    credit, _sep, title = stem.rpartition(" - ")
    tw = _words(title or stem)
    ctx = set(_words(album)) | set(_words(artist)) | set(_words(credit)) | set(tw)
    if not qw or not tw:
        return 0.0
    hit = sum(1 for q in qw if q in ctx)
    if hit == 0:
        return 0.0
    cover = hit / len(qw)
    plain = [w for w in tw if w not in _VERSION_WORDS and not re.fullmatch(r"\d{2,4}", w)]
    title_hit = sum(1 for w in plain if w in set(qw)) / max(1, len(plain))   # how much of the title you said
    fuzzy = SequenceMatcher(None, " ".join(qw), " ".join(tw)).ratio()
    # the original over a remix / live cut / karaoke track — unless you asked for that version
    extra = [w for w in tw if w in _VERSION_WORDS and w not in set(qw)]
    return 0.55 * cover + 0.3 * title_hit + 0.15 * fuzzy - 0.08 * len(extra)


def _rank(qw: list[str], paths, limit: int = 8) -> list[tuple[float, str]]:
    scored = []
    for p in paths:
        s = _track_score(qw, p)
        if s >= 0.6:
            scored.append((s, p))
    scored.sort(key=lambda t: (-t[0], len(t[1])))
    return scored[:limit]


# ---- artist folders (fast path) -------------------------------------------------------
def _scan_dirs(root: str) -> list[str]:
    try:
        with os.scandir(root) as it:
            return [e.name for e in it if e.is_dir()]
    except OSError:
        return []


def artist_folders(root: str) -> list[str]:
    hit = _artists.get(root)
    if hit and time.time() - hit[0] < _ARTIST_TTL:
        return hit[1]
    disk = _load_index()
    if disk and disk.get("artists", {}).get(root):
        names = disk["artists"][root]
        _artists[root] = (float(disk.get("built", 0)), names)
        if time.time() - float(disk.get("built", 0)) < _ARTIST_TTL:
            return names
    names = _scan_dirs(root) or (hit[1] if hit else [])
    _artists[root] = (time.time(), names)
    return names


def _artist_match(query: str, root: str) -> tuple[str, str] | None:
    """('artist folder', 'rest of the query') when the query names an artist."""
    q = (query or "").lower()
    m = re.search(r"^(.*?)\s+by\s+(.+)$", q)
    names = artist_folders(root)
    if m:
        want = _words(m.group(2))
        best, score = None, 0.0
        for n in names:
            nw = _words(n)
            if not nw:
                continue
            hit = sum(1 for w in want if w in set(nw))
            s = hit / max(len(want), len(nw))
            if s > score:
                best, score = n, s
        if best and score >= 0.5:
            return best, m.group(1)
        return None
    # no "by": an artist name that's all of the leading or trailing words ("michael jackson thriller")
    qw = _words(q)
    for n in names:
        nw = _words(n)
        # short one-word artist names ("-M-", "Yes") only count after an explicit "by"
        if len(" ".join(nw)) < 5:
            continue
        if len(nw) >= 1 and len(qw) > len(nw):
            if qw[:len(nw)] == nw:
                return n, " ".join(qw[len(nw):])
            if qw[-len(nw):] == nw:
                return n, " ".join(qw[:-len(nw)])
    return None


def _walk_audio(folder: str):
    for dp, _dn, fns in os.walk(folder):
        for fn in fns:
            if fn.lower().endswith(AUDIO_EXT):
                yield os.path.join(dp, fn)


# ---- the full index -------------------------------------------------------------------
def _index_path():
    try:
        from .musiclib import player      # (Ava's data folder, or the music library's own when a service set one)

        return player.data_dir() / "music_index.json"
    except Exception:
        return None


def _load_index() -> dict | None:
    global _index
    if _index is not None:
        return _index
    p = _index_path()
    if not p or not p.exists():
        return None
    try:
        _index = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        _index = None
    return _index


def index_ready() -> bool:
    d = _load_index()
    return bool(d and d.get("files"))


def build_index(cfg) -> int:
    """Walk every music folder and save the track list (minutes on a big network library)."""
    global _index
    if _building.is_set():
        return 0
    _building.set()
    try:
        files, artists = [], {}
        for root in folders(cfg):
            if not os.path.isdir(root):
                continue
            artists[root] = _scan_dirs(root)
            files.extend(_walk_audio(root))
        data = {"built": time.time(), "files": files, "artists": artists}
        p = _index_path()
        if p:
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, p)
        with _lock:
            _index = data
        log.info("Music index built: %d tracks", len(files))
        return len(files)
    except Exception as exc:
        log.warning("Music index failed: %s", exc)
        return 0
    finally:
        _building.clear()


def ensure_index(cfg) -> None:
    """Start a background (re)build when there's no index or it's a day old."""
    d = _load_index()
    if d and time.time() - float(d.get("built", 0)) < _INDEX_TTL:
        return
    if not _building.is_set():
        threading.Thread(target=build_index, args=(cfg,), daemon=True, name="music-index").start()


# ---- find + play ----------------------------------------------------------------------
def find_song_scored(cfg, query: str, limit: int = 8) -> tuple[list[tuple[float, str]], str]:
    """([(score, file)] best first, how: 'artist' | 'index' | 'indexing' | 'none')."""
    roots = [r for r in folders(cfg) if os.path.isdir(r)]
    if not roots:
        return [], "none"
    for root in roots:
        am = _artist_match(query, root)
        if am:
            artist, rest = am
            qw = _words(rest) or _words(artist)
            ranked = _rank(qw + _words(artist) if rest else qw, _walk_audio(os.path.join(root, artist)), limit)
            if ranked:
                return ranked, "artist"
    d = _load_index()
    if d and d.get("files"):
        ranked = _rank(_words(query), d["files"], limit)
        ensure_index(cfg)                       # refresh in the background when it's old
        return ranked, "index"
    ensure_index(cfg)
    return [], "indexing"


def find_song(cfg, query: str, limit: int = 8) -> tuple[list[str], str]:
    """(matching files best first, how: 'artist' | 'index' | 'indexing' | 'none')."""
    ranked, how = find_song_scored(cfg, query, limit)
    return [p for _s, p in ranked], how


TIE = 0.01          # two scores this close are a tie: ask, don't guess


def _title(path: str) -> str:
    stem = re.sub(r"^\d+[\s.\-_]+", "", os.path.splitext(os.path.basename(path))[0])
    return stem.rpartition(" - ")[2] or stem


def label(path: str) -> str:
    """'D:\\Music\\Michael Jackson\\Thriller\\Michael Jackson - Billie Jean.mp3' -> 'Billie Jean from Thriller'."""
    album = os.path.basename(os.path.dirname(path))
    return f"{_title(path)} from {album}" if album else _title(path)


def tie_question(paths: list[str]) -> str:
    a, b = label(paths[0]), label(paths[1])
    if a == b:
        a = f"{a} by {os.path.basename(os.path.dirname(os.path.dirname(paths[0])))}"
        b = f"{b} by {os.path.basename(os.path.dirname(os.path.dirname(paths[1])))}"
    return f"{a}, or {b}?"


def play_in_winamp(path: str, cfg=None) -> dict:
    import subprocess

    if not path or not os.path.isfile(path):
        return {"ok": False, "say": "I couldn't find that song file."}
    name = re.sub(r"^\d+[\s.\-_]+", "", os.path.splitext(os.path.basename(path))[0])
    player = winamp_path(cfg)
    try:
        if player:
            subprocess.Popen([player, path], close_fds=True)
            where = "Winamp"
        else:
            os.startfile(path)  # noqa: S606 — your default music player
            where = "your music player"
        try:                        # "add this song to road trip" / "i love this" know which song
            from .musiclib import playlists

            playlists.set_session(kind="song", path=path, label=name)
        except Exception:
            pass
        return {"ok": True, "say": f"Playing {name} in {where}.", "path": path}
    except Exception as exc:
        return {"ok": False, "say": f"I found {name} but couldn't start it: {exc}"}


def play_song(cfg, query: str, ask_on_tie: bool = True, found=None) -> dict:
    """`found` = (ranked, how) from find_song_scored, when the caller already searched."""
    ranked, how = found if found is not None else find_song_scored(cfg, query)
    if how == "none":
        return {"ok": False, "say": "I can't see your music folder. Set it on the Home Lab page, under Media.",
                "matches": []}
    if how == "indexing":
        return {"ok": False, "matches": [],
                "say": "I'm still learning your music library — the first time takes a few minutes. "
                       "Say the artist too, like 'play Billie Jean by Michael Jackson', and I'll find it now."}
    if not ranked:
        return {"ok": False, "say": f"I couldn't find a song matching {query}.", "matches": []}
    matches, scores = [p for _s, p in ranked], [round(s, 3) for s, _p in ranked]
    if ask_on_tie and len(scores) >= 2 and scores[0] - scores[1] < TIE:
        return {"ok": True, "tie": True, "say": tie_question(matches), "matches": matches, "scores": scores}
    r = play_in_winamp(matches[0], cfg)
    r["matches"], r["scores"] = matches, scores
    return r
