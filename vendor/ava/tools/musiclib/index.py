"""The tag index: every track's artist / album / title / genre / year / track number / length, in SQLite.

Built in a background thread with mutagen, folder by folder (an artist folder at a time), so a big network library
(a quarter million tracks) is never a wait:
  * incremental — a file whose size and modified time haven't changed is never read again;
  * resumable — where the pass got to is saved after every folder, so a restart carries on from there;
  * useful while it builds — each folder's files go in first from their FOLDER names (Artist\\Album (1997)\\03 - Title),
    then their tags replace that. A folder no pass has reached yet is read from its names on the spot when you ask for
    that artist.
Ava only reads the files; the index lives in her data folder (music_library.db). Files that vanish leave the index at
the end of a pass that saw their whole music folder (an unplugged share never empties it).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from collections import deque

from ...log import get
from . import vocab


_RXC: dict = {}


def _rx(p: str):
    """A compiled pattern from this module's own cache (never Python's shared 512-entry re cache, which the
    router's phrasebook already fills)."""
    r = _RXC.get(p)
    if r is None:
        r = _RXC[p] = re.compile(p)
    return r


log = get()

AUDIO_EXT = (".mp3", ".flac", ".m4a", ".wav", ".ogg", ".wma", ".aac", ".opus", ".aif", ".aiff", ".ape", ".wv",
             ".mpc", ".alac", ".m4b", ".oga", ".mp4a")
DB_NAME = "music_library.db"
REFRESH_TTL = 24 * 3600             # a finished pass is redone (incrementally) once a day
_VARIOUS = re.compile(r"^(?:various(?: artists?)?|va|v\.a\.|compilations?|soundtracks?|unknown(?: artist)?|"
                      r"misc(?:ellaneous)?|singles?|mixtapes?|playlists?|downloads?|new music|_?unsorted)$", re.I)
_DISC_DIR = re.compile(r"^(?:cd|disc|disk|dvd)\s*[-_.]?\s*(\d{1,2})\b", re.I)
_FEAT = re.compile(r"\s*[\(\[]?\b(?:feat\.?|ft\.?|featuring|with)\s+([^\)\]]+)[\)\]]?", re.I)

_gen_lock = threading.Lock()
_changes = 0                         # bumped whenever the index changes (caches compare it)


def changed() -> int:
    return _changes


def _bump():
    global _changes
    with _gen_lock:
        _changes += 1


# ---------------------------------------------------------------- the database
def db_path():
    from . import player

    return player.data_dir() / DB_NAME


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks(
  path TEXT PRIMARY KEY, root TEXT, folder_artist TEXT, artist TEXT, albumartist TEXT,
  main_artist TEXT, main_key TEXT, primary_key TEXT, credit_key TEXT,
  album TEXT, album_key TEXT, title TEXT, title_key TEXT,
  genre TEXT, genre_keys TEXT, year INTEGER, year_src TEXT,
  track INTEGER, disc INTEGER, duration REAL, bpm INTEGER,
  mtime REAL, size INTEGER, tagged INTEGER DEFAULT 0, gen INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_main ON tracks(main_key);
CREATE INDEX IF NOT EXISTS ix_primary ON tracks(primary_key);
CREATE INDEX IF NOT EXISTS ix_album ON tracks(album_key);
CREATE INDEX IF NOT EXISTS ix_year ON tracks(year);
CREATE INDEX IF NOT EXISTS ix_root ON tracks(root, gen);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS feedback(path TEXT PRIMARY KEY, loves INTEGER DEFAULT 0, skips INTEGER DEFAULT 0,
  plays INTEGER DEFAULT 0, banned INTEGER DEFAULT 0, ts REAL);
CREATE TABLE IF NOT EXISTS moods(path TEXT, mood TEXT, fit REAL, src TEXT, ts REAL, PRIMARY KEY(path, mood));
"""


def connect(path=None) -> sqlite3.Connection:
    p = str(path or db_path())
    con = sqlite3.connect(p, timeout=20, check_same_thread=False)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.DatabaseError:
        pass
    con.executescript(_SCHEMA)
    return con


def meta_get(con, k: str, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r["v"])
    except Exception:
        return default


def meta_set(con, k: str, v) -> None:
    con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, json.dumps(v)))


# ---------------------------------------------------------------- reading one file
def _num(v) -> int | None:
    m = _rx(r"\s*(\d{1,4})").match(str(v or ""))
    return int(m.group(1)) if m else None


_TAGKEYS = {
    "artist": ("artist", "tpe1", "author", "\xa9art"),
    "albumartist": ("albumartist", "album artist", "album_artist", "tpe2", "wm/albumartist", "aart", "band"),
    "album": ("album", "talb", "wm/albumtitle", "\xa9alb"),
    "title": ("title", "tit2", "\xa9nam"),
    "genre": ("genre", "tcon", "wm/genre", "\xa9gen"),
    "date": ("date", "originaldate", "year", "tdrc", "tyer", "tdor", "wm/year", "wm/originalreleaseyear", "\xa9day"),
    "track": ("tracknumber", "track", "trck", "wm/tracknumber", "trkn"),
    "disc": ("discnumber", "disc", "tpos", "wm/partofset", "disk"),
    "bpm": ("bpm", "tbpm", "tempo", "wm/beatsperminute", "tmpo"),
}


def _vals(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        items = v
    elif hasattr(v, "text"):                 # an ID3 frame
        items = list(getattr(v, "text") or [])
    else:
        items = [v]
    out = []
    for x in items:
        if isinstance(x, tuple):             # MP4 trkn/disk: (3, 12)
            x = x[0]
        s = str(x).strip().strip("\x00")
        if s:
            out.append(s)
    return out


def read_tags(path: str) -> dict | None:
    """The file's tags via mutagen -> {artist, albumartist, album, title, genre, date, track, disc, bpm, duration}.
    None when it can't be read (not audio, damaged, mutagen missing)."""
    try:
        import mutagen
    except Exception:                        # (the installed app ships it; a bare dev venv may not)
        return None
    try:
        f = mutagen.File(path, easy=True)
    except Exception:
        f = None
    if f is None:
        try:
            f = mutagen.File(path)
        except Exception:
            return None
    if f is None:
        return None
    tags = getattr(f, "tags", None)
    low: dict = {}
    try:
        if tags is not None:
            for k in list(tags.keys()):
                try:
                    low.setdefault(str(k).lower(), tags[k])
                except Exception:
                    continue
    except Exception:
        low = {}
    out: dict = {}
    for field, keys in _TAGKEYS.items():
        vals: list[str] = []
        for k in keys:
            if k in low:
                vals = _vals(low[k])
                if vals:
                    break
        if field == "genre":
            out[field] = "; ".join(dict.fromkeys(vals))
        elif field == "artist":
            out[field] = " & ".join(dict.fromkeys(vals))
        else:
            out[field] = vals[0] if vals else ""
    try:
        out["duration"] = float(getattr(getattr(f, "info", None), "length", 0.0) or 0.0)
    except Exception:
        out["duration"] = 0.0
    return out


# ---------------------------------------------------------------- folder names (the fallback)
def _primary(credit: str) -> str:
    """'Joyner Lucas feat. Logic' / 'Eminem, Rihanna' / 'Drake x Future' -> 'Joyner Lucas' / 'Eminem' / 'Drake'.
    ('Simon & Garfunkel' stays whole: & and 'and' join duos.)"""
    c = _FEAT.split(credit or "")[0]
    c = _rx(r"(?i)\s*[,;/]\s*|\s+(?:x|vs\.?|with)\s+").split(c)[0]
    return c.strip()


def parse_path(path: str, root: str) -> dict:
    """What the path alone says: D:\\Music\\Joyner Lucas\\ADHD (2020)\\03 - Joyner Lucas & Logic - Isis.mp3 ->
    folder_artist Joyner Lucas, album ADHD, year 2020, track 3, credit 'Joyner Lucas & Logic', title Isis."""
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        rel = os.path.basename(path)
    parts = [p for p in _rx(r"[\\/]+").split(rel) if p and p != "."]
    fname = parts[-1] if parts else os.path.basename(path)
    dirs = parts[:-1]
    disc = None
    if dirs and _DISC_DIR.match(dirs[-1]):
        disc = int(_DISC_DIR.match(dirs[-1]).group(1))
        dirs = dirs[:-1]
    folder_artist = dirs[0] if len(dirs) >= 1 else ""
    album_dir = dirs[-1] if len(dirs) >= 2 else ""
    album, year = vocab.folder_year(album_dir) if album_dir else ("", None)
    stem = os.path.splitext(fname)[0]
    track = None
    m = _rx(r"^\s*(?:(\d)[-.](\d{1,3})|(\d{1,3}))(?:\s*[-._]\s*|\s+)(?=\S)").match(stem)
    if m:
        if m.group(1):
            disc = disc or int(m.group(1))
            track = int(m.group(2))
        else:
            track = int(m.group(3))
        stem = stem[m.end():]
    credit, sep, title = stem.rpartition(" - ")
    if not sep:
        credit, title = "", stem
    credit = credit.strip()
    if credit and album and vocab.akey(credit) == vocab.akey(album):      # "Album - Title"
        credit = ""
    title = title.strip()
    return {"folder_artist": folder_artist, "album": album, "year": year, "track": track, "disc": disc,
            "credit": credit, "title": title or stem}


def make_row(path: str, root: str, mtime: float, size: int, tags: dict | None, gen: int) -> dict:
    fb = parse_path(path, root)
    t = tags or {}
    artist = (t.get("artist") or fb["credit"] or fb["folder_artist"] or "").strip()
    albumartist = (t.get("albumartist") or "").strip()
    title = (t.get("title") or fb["title"] or "").strip()
    album = (t.get("album") or fb["album"] or "").strip()
    year, ysrc = vocab.year_from_text(t.get("date") or ""), "tag"
    if year is None and fb["year"]:
        year, ysrc = fb["year"], "folder"
    if year is None:
        ysrc = ""
    fa = fb["folder_artist"]
    fa_shown = _rx(r"^[^\w(]+").sub("", fa).strip() or fa          # ("!Snap" is sorted to the top; it's Snap)
    prim0 = _primary(artist)
    if albumartist and not _VARIOUS.match(albumartist):
        main = albumartist
    elif fa and not _VARIOUS.match(fa):
        # the folder says who; the tag spells it best when it's the same name ("Bebe & Cece" -> "BeBe & CeCe")
        main = prim0 if prim0 and vocab.akey(prim0) == vocab.akey(fa_shown) else fa_shown
    else:
        main = prim0 or fa_shown or albumartist
    prim = prim0 or main
    feats = " ".join(a or b for a, b in vocab.TITLE_FEAT_RX.findall(title or ""))
    credit_key = " " + " ".join(x for x in (vocab.akey(artist), vocab.akey(feats)) if x) + " "
    genre = (t.get("genre") or "").strip()
    return {
        "path": path, "root": root, "folder_artist": fa, "artist": artist, "albumartist": albumartist,
        "main_artist": main, "main_key": vocab.akey(main), "primary_key": vocab.akey(prim), "credit_key": credit_key,
        "album": album, "album_key": vocab.akey(album), "title": title, "title_key": vocab.song_key(title),
        "genre": genre, "genre_keys": vocab.genre_field(genre), "year": year, "year_src": ysrc,
        "track": _num(t.get("track")) or fb["track"], "disc": _num(t.get("disc")) or fb["disc"],
        "duration": float(t.get("duration") or 0.0) or None, "bpm": _num(t.get("bpm")),
        "mtime": float(mtime or 0.0), "size": int(size or 0), "tagged": 1 if tags is not None else 0, "gen": gen,
    }


_COLS = ("path", "root", "folder_artist", "artist", "albumartist", "main_artist", "main_key", "primary_key",
         "credit_key", "album", "album_key", "title", "title_key", "genre", "genre_keys", "year", "year_src", "track",
         "disc", "duration", "bpm", "mtime", "size", "tagged", "gen")
_UPSERT = (f"INSERT OR REPLACE INTO tracks({', '.join(_COLS)}) VALUES({', '.join('?' * len(_COLS))})")


def upsert(con, rows) -> None:
    con.executemany(_UPSERT, [tuple(r[c] for c in _COLS) for r in rows])


def insert_missing(con, rows) -> None:
    """Folder-name rows for files the index doesn't have yet (never over a row whose tags were read)."""
    con.executemany(_UPSERT.replace("INSERT OR REPLACE", "INSERT OR IGNORE"), [tuple(r[c] for c in _COLS) for r in rows])


# ---------------------------------------------------------------- walking a folder
def _walk(top: str):
    """(path, mtime, size) of every audio file under top (DirEntry.stat is free on Windows: no extra round trip)."""
    stack = [top]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        for e in sorted(entries, key=lambda x: x.name.lower()):
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif e.name.lower().endswith(AUDIO_EXT):
                    st = e.stat()
                    yield e.path, st.st_mtime, st.st_size
            except OSError:
                continue


def folder_rows(folder: str, root: str) -> list[dict]:
    """Rows for a folder from its file names alone, read on the spot (an artist folder no pass has reached yet)."""
    out = []
    for p, mt, sz in _walk(folder):
        out.append(make_row(p, root, mt, sz, None, 0))
    return out


# ---------------------------------------------------------------- the background pass
class Indexer:
    """One background pass at a time over the music folders. start() is cheap and safe to call often."""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._priority: deque = deque()
        self.current = ""
        self.read = read_tags            # (a test seam)
        self.pause = 0.02                # between folders: gentle on the NAS

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def prioritize(self, folder: str, root: str = "") -> None:
        """Read this folder's tags next (you just asked for that artist): queued when a pass is running, else read
        now in a short background thread of its own."""
        with self._lock:
            if self.running():
                if folder not in self._priority:
                    self._priority.appendleft(folder)
                return
        if not root:
            return

        def one():
            try:
                con = connect()
                try:
                    gen = int(meta_get(con, "gen", 0) or 0)
                    _do_folder(con, root, list(_walk(folder)), gen, self.read, self._stop)
                finally:
                    con.close()
            except Exception as exc:
                log.debug("music index: folder %s failed: %s", folder, exc)
        threading.Thread(target=one, daemon=True, name="music-tags-folder").start()

    def stop(self) -> None:
        self._stop.set()

    def due(self, roots) -> bool:
        try:
            con = connect()
            try:
                if meta_get(con, "cursor"):
                    return True                  # a pass was interrupted: carry on
                last = float(meta_get(con, "last_full", 0) or 0)
            finally:
                con.close()
        except Exception:
            return True
        return time.time() - last > REFRESH_TTL

    def start(self, roots, force: bool = False) -> bool:
        roots = [r for r in roots if r]
        with self._lock:
            if self.running():
                return False
            if not force and not self.due(roots):
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, args=(list(roots),), daemon=True, name="music-tags")
            self._thread.start()
            return True

    def _run(self, roots):
        try:
            scan(roots, stop=self._stop, indexer=self)
        except Exception as exc:
            log.warning("music tag index stopped: %s", exc)
        finally:
            self.current = ""


INDEXER = Indexer()


def _seed_from_filelist(con, roots, gen: int) -> int:
    """First run: the old file-name index (music_index.json, one walk of every file) seeds the rows at once, so
    artists / albums / years from folder names work before a single tag is read."""
    try:
        from .. import music as old

        d = old._load_index()
    except Exception:
        d = None
    files = (d or {}).get("files") or []
    if not files:
        return 0
    rows = []
    roots_l = [(r, r.lower().rstrip("\\/") + os.sep) for r in roots]
    for p in files:
        pl = p.lower()
        root = next((r for r, rl in roots_l if pl.startswith(rl)), None)
        if not root:
            continue
        rows.append(make_row(p, root, 0.0, 0, None, gen))
        if len(rows) >= 5000:
            upsert(con, rows)
            rows = []
    if rows:
        upsert(con, rows)
    con.commit()
    _bump()
    return len(files)


def _top_dirs(root: str) -> list[str]:
    try:
        with os.scandir(root) as it:
            return sorted((e.path for e in it if e.is_dir(follow_symlinks=False)), key=lambda p: p.lower())
    except OSError:
        return []


def _root_files(root: str) -> list[tuple[str, float, int]]:
    out = []
    try:
        with os.scandir(root) as it:
            for e in it:
                if e.is_file() and e.name.lower().endswith(AUDIO_EXT):
                    st = e.stat()
                    out.append((e.path, st.st_mtime, st.st_size))
    except OSError:
        pass
    return out


def _do_folder(con, root: str, files, gen: int, read, stop) -> int:
    """Index one folder's files: new/changed ones in from their names at once, then their tags. -> tags read."""
    if not files:
        return 0
    paths = [f[0] for f in files]
    have = {}
    for i in range(0, len(paths), 500):
        chunk = paths[i:i + 500]
        q = "SELECT path, mtime, size, tagged FROM tracks WHERE path IN (%s)" % ",".join("?" * len(chunk))
        for r in con.execute(q, chunk):
            have[r["path"]] = (r["mtime"], r["size"], r["tagged"])
    todo, seen, quick = [], [], []
    for p, mt, sz in files:
        h = have.get(p)
        if h and abs((h[0] or 0) - mt) < 1.0 and int(h[1] or 0) == int(sz) and int(h[2] or 0) in (1, 2):
            seen.append(p)                                   # unchanged and already tagged (or unreadable)
            continue
        if not h or int(h[2] or 0) != 1:
            quick.append(make_row(p, root, mt, sz, None, gen))
        todo.append((p, mt, sz))
    if seen:
        con.executemany("UPDATE tracks SET gen=? WHERE path=?", [(gen, p) for p in seen])
    if quick:
        for r in quick:
            r["mtime"] = 0.0                                 # (not tagged yet: a later pass must read it)
        upsert(con, quick)
    con.commit()
    if quick:
        _bump()
    n = 0
    batch = []
    for p, mt, sz in todo:
        if stop is not None and stop.is_set():
            break
        tags = read(p)
        row = make_row(p, root, mt, sz, tags, gen)
        if tags is None:
            row["tagged"] = 2                               # couldn't read it: don't retry until it changes
        batch.append(row)
        n += 1
        if len(batch) >= 50:
            upsert(con, batch)
            con.commit()
            batch = []
            _bump()
    if batch:
        upsert(con, batch)
        con.commit()
        _bump()
    return n


def scan(roots, stop: threading.Event | None = None, indexer: Indexer | None = None, read=None) -> dict:
    """One full (incremental, resumable) pass over the music folders. -> counts."""
    read = read or (indexer.read if indexer else read_tags)
    con = connect()
    stats = {"folders": 0, "read": 0, "roots": 0, "done": False}
    try:
        cur = meta_get(con, "cursor") or {}
        if cur.get("gen"):
            gen = int(cur["gen"])
        else:
            gen = int(meta_get(con, "gen", 0) or 0) + 1
            meta_set(con, "gen", gen)
            cur = {"gen": gen, "root": "", "dir": "", "done_roots": []}
            meta_set(con, "cursor", cur)
            con.commit()
        if not con.execute("SELECT 1 FROM tracks LIMIT 1").fetchone():
            n = _seed_from_filelist(con, roots, gen)
            if n:
                log.info("music index: seeded %d tracks from the file list", n)
        done_roots = list(cur.get("done_roots") or [])
        complete = []
        for root in roots:
            if root in done_roots:
                complete.append(root)
                continue
            if not os.path.isdir(root):
                continue                                         # unreachable: never pruned this pass
            stats["roots"] += 1
            dirs = _top_dirs(root)
            start_after = cur.get("dir", "") if cur.get("root") == root else ""
            finished = True
            # loose files right in the root first
            if not start_after:
                stats["read"] += _do_folder(con, root, _root_files(root), gen, read, stop)
            for d in dirs:
                if stop is not None and stop.is_set():
                    finished = False
                    break
                # a folder you just asked for jumps the queue
                while indexer is not None and indexer._priority:
                    with indexer._lock:
                        pd = indexer._priority.popleft() if indexer._priority else None
                    if pd and os.path.isdir(pd):
                        indexer.current = pd
                        stats["read"] += _do_folder(con, root, list(_walk(pd)), gen, read, stop)
                if start_after and d.lower() <= start_after.lower():
                    continue
                if indexer is not None:
                    indexer.current = d
                stats["read"] += _do_folder(con, root, list(_walk(d)), gen, read, stop)
                stats["folders"] += 1
                if stop is not None and stop.is_set():
                    finished = False
                    break
                cur.update({"root": root, "dir": d})
                meta_set(con, "cursor", cur)
                con.commit()
                if indexer is not None and indexer.pause:
                    time.sleep(indexer.pause)
            if not finished:
                return stats
            done_roots.append(root)
            complete.append(root)
            cur.update({"root": "", "dir": "", "done_roots": done_roots})
            meta_set(con, "cursor", cur)
            con.commit()
        # every reachable folder seen: files that vanished leave the index
        for root in complete:
            con.execute("DELETE FROM tracks WHERE root=? AND gen<?", (root, gen))
        con.execute("DELETE FROM meta WHERE k='cursor'")
        meta_set(con, "last_full", time.time())
        con.commit()
        _bump()
        stats["done"] = True
        log.info("music index pass done: %d folders, %d files read", stats["folders"], stats["read"])
        return stats
    finally:
        con.close()


def counts() -> dict:
    try:
        con = connect()
    except Exception:
        return {"tracks": 0, "tagged": 0, "artists": 0, "last_full": 0, "building": INDEXER.running()}
    try:
        r = con.execute("SELECT COUNT(*) n, SUM(tagged=1) t, COUNT(DISTINCT main_key) a FROM tracks").fetchone()
        return {"tracks": int(r["n"] or 0), "tagged": int(r["t"] or 0), "artists": int(r["a"] or 0),
                "last_full": float(meta_get(con, "last_full", 0) or 0), "building": INDEXER.running(),
                "resuming": bool(meta_get(con, "cursor"))}
    finally:
        con.close()
